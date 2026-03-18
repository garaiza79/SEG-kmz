"""
Creación de KMZ del SEG - CFE
=======================================================
Esta app convierte un archivo KMZ con cualquier estructura
al formato requerido por el SEG (Sistema Electrónico de Gestión)
de la Comisión Federal de Electricidad.

Estructura requerida por el SEG:
    Solicitud
    └── Ruta 1
        ├── Trayectoria
        │   └── (líneas)
        └── Postes
            └── (puntos)
"""

import streamlit as st
import zipfile
import xml.etree.ElementTree as ET
from io import BytesIO
import re
import copy

# ─────────────────────────────────────────────────────────────
# CONFIGURACIÓN INICIAL DE NAMESPACES
# ─────────────────────────────────────────────────────────────
# Esto le dice a Python cómo nombrar los "prefijos" del XML al serializar.
# Si no hacemos esto, Python agrega prefijos feos como "ns0:", "ns1:", etc.
ET.register_namespace("", "http://www.opengis.net/kml/2.2")
ET.register_namespace("gx", "http://www.google.com/kml/ext/2.2")
ET.register_namespace("atom", "http://www.w3.org/2005/Atom")

# Namespace de KML - lo usamos mucho, lo guardamos en una variable
KML = "http://www.opengis.net/kml/2.2"
GX  = "http://www.google.com/kml/ext/2.2"


# ─────────────────────────────────────────────────────────────
# FUNCIÓN 1: LEER EL KMZ
# ─────────────────────────────────────────────────────────────
def parse_kmz(file_bytes):
    """
    Lee un archivo KMZ y extrae todos los Placemarks (puntos y líneas).

    Un KMZ es básicamente un archivo ZIP que contiene un archivo .kml adentro.
    El .kml es un XML que describe la geografía (puntos, líneas, carpetas, etc.)

    Retorna una lista de diccionarios, cada uno con:
        - id:       número único del elemento
        - name:     nombre del placemark (ej. "Poste 1", "Trayectoria UM")
        - tipo:     "Línea" o "Punto"
        - path:     ruta de carpetas donde estaba (ej. "Proyecto / Postes / CFE")
        - element:  el objeto XML original (lo guardamos para reusarlo después)
    """
    # Abrir el KMZ como un archivo ZIP
    with zipfile.ZipFile(BytesIO(file_bytes)) as z:
        # Buscar el archivo .kml dentro del ZIP
        kml_file = next(
            (name for name in z.namelist() if name.endswith(".kml")), None
        )
        if kml_file is None:
            raise ValueError("No se encontró un archivo .kml dentro del KMZ.")

        kml_content = z.read(kml_file)

    # Parsear el XML del KML
    root = ET.fromstring(kml_content)

    # Buscar el elemento <Document> que es el contenedor principal
    document = root.find(f"{{{KML}}}Document")
    if document is None:
        document = root  # Si no hay Document, usar el root directamente

    placemarks = []
    contador = [0]  # Lista para poder modificarla dentro de la función anidada

    def extraer_placemarks(elemento, ruta_actual=""):
        """
        Función interna que recorre el árbol XML de forma recursiva.
        Entra a cada Folder y extrae cada Placemark que encuentre.
        """
        for hijo in elemento:
            # Quitar el namespace del tag para obtener solo el nombre
            # Ejemplo: "{http://www.opengis.net/kml/2.2}Folder" → "Folder"
            tag = hijo.tag.replace(f"{{{KML}}}", "")

            if tag in ("Folder", "Document"):
                # Es una carpeta: obtener su nombre y entrar recursivamente
                nombre_carpeta = hijo.find(f"{{{KML}}}name")
                nombre_text = nombre_carpeta.text if nombre_carpeta is not None else "Sin nombre"
                nueva_ruta = f"{ruta_actual} / {nombre_text}" if ruta_actual else nombre_text
                extraer_placemarks(hijo, nueva_ruta)

            elif tag == "Placemark":
                # Es un placemark: extraer su información
                nombre_el = hijo.find(f"{{{KML}}}name")
                nombre = nombre_el.text if nombre_el is not None else "Sin nombre"

                # Determinar si es línea o punto
                if hijo.find(f"{{{KML}}}LineString") is not None:
                    tipo = "Línea"
                elif hijo.find(f"{{{KML}}}Point") is not None:
                    tipo = "Punto"
                else:
                    tipo = "Otro"

                contador[0] += 1
                placemarks.append({
                    "id": contador[0],
                    "name": nombre,
                    "tipo": tipo,
                    "path": ruta_actual,
                    "element": hijo,
                })

    extraer_placemarks(document)
    return placemarks


# ─────────────────────────────────────────────────────────────
# FUNCIÓN 2: CONVERTIR ELEMENTO XML A STRING LIMPIO
# ─────────────────────────────────────────────────────────────
def elemento_a_string(elemento):
    """
    Convierte un elemento XML a texto, limpiando los prefijos de namespace.

    Cuando Python serializa un elemento XML, a veces agrega prefijos como
    'kml:' o 'ns0:' en los tags. Esta función los limpia para que el
    XML resultante sea compatible con Google Earth y el SEG.
    """
    texto = ET.tostring(elemento, encoding="unicode")

    # Quitar declaraciones de namespace extra (xmlns="..." xmlns:gx="...")
    # que Python a veces agrega en el elemento raíz al serializarlo por separado
    texto = re.sub(r' xmlns(?::\w+)?="[^"]*"', "", texto)

    # Quitar el prefijo "kml:" de los tags si aparece
    # Ejemplo: <kml:Placemark> → <Placemark>
    texto = re.sub(r"<kml:", "<", texto)
    texto = re.sub(r"</kml:", "</", texto)

    return texto


# ─────────────────────────────────────────────────────────────
# FUNCIÓN 3: GENERAR EL KMZ CON ESTRUCTURA SEG
# ─────────────────────────────────────────────────────────────
def generar_kmz(rutas_config, placemarks_por_id, nombre_proyecto):
    """
    Genera un nuevo archivo KMZ con la estructura correcta para el SEG.

    La estructura resultante será:
        Solicitud
        ├── Ruta 1
        │   ├── Trayectoria
        │   │   └── (líneas seleccionadas)
        │   └── Postes
        │       └── (puntos seleccionados)
        ├── Ruta 2
        │   └── ...
        └── Ruta N

    Parámetros:
        rutas_config   : dict {ruta_num: {'trayectoria': [ids], 'postes': [ids]}}
        placemarks_por_id : dict {id: placemark_dict}
        nombre_proyecto: string con el nombre del proyecto
    """

    # ── Construir el XML de cada Ruta ──────────────────────────
    xml_rutas = []

    for num_ruta, datos in rutas_config.items():

        # Construir los Placemarks de Trayectoria
        xml_trayectorias = ""
        for pm_id in datos["trayectoria"]:
            if pm_id in placemarks_por_id:
                pm = placemarks_por_id[pm_id]["element"]
                pm_str = elemento_a_string(pm)
                # Reemplazar el estilo por el estilo de trayectoria
                pm_str = re.sub(
                    r"<styleUrl>[^<]*</styleUrl>",
                    "<styleUrl>#estilo_trayectoria</styleUrl>",
                    pm_str
                )
                xml_trayectorias += f"\t\t\t\t{pm_str}\n"

        # Construir los Placemarks de Postes
        xml_postes = ""
        for pm_id in datos["postes"]:
            if pm_id in placemarks_por_id:
                pm = placemarks_por_id[pm_id]["element"]
                pm_str = elemento_a_string(pm)
                # Reemplazar el estilo por el estilo de poste
                pm_str = re.sub(
                    r"<styleUrl>[^<]*</styleUrl>",
                    "<styleUrl>#estilo_poste</styleUrl>",
                    pm_str
                )
                xml_postes += f"\t\t\t\t{pm_str}\n"

        # Armar el XML de esta Ruta
        xml_ruta = f"""		<Folder>
			<name>Ruta {num_ruta}</name>
			<open>1</open>
			<Folder>
				<name>Trayectoria</name>
{xml_trayectorias}			</Folder>
			<Folder>
				<name>Postes</name>
{xml_postes}			</Folder>
		</Folder>"""

        xml_rutas.append(xml_ruta)

    # ── Unir todas las Rutas ───────────────────────────────────
    todas_las_rutas = "\n".join(xml_rutas)

    # ── Construir el KML completo ──────────────────────────────
    kml_completo = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"
     xmlns:gx="http://www.google.com/kml/ext/2.2"
     xmlns:kml="http://www.opengis.net/kml/2.2"
     xmlns:atom="http://www.w3.org/2005/Atom">
<Document>
	<name>{nombre_proyecto}</name>

	<!-- Estilo para las líneas de trayectoria -->
	<Style id="estilo_trayectoria">
		<IconStyle>
			<scale>1.1</scale>
			<Icon>
				<href>http://maps.google.com/mapfiles/kml/pushpin/ylw-pushpin.png</href>
			</Icon>
			<hotSpot x="20" y="2" xunits="pixels" yunits="pixels"/>
		</IconStyle>
		<LineStyle>
			<color>ffff0000</color>
			<width>3</width>
		</LineStyle>
	</Style>

	<!-- Estilo para los postes (puntos) -->
	<Style id="estilo_poste">
		<IconStyle>
			<Icon>
				<href>http://maps.google.com/mapfiles/kml/pushpin/ylw-pushpin.png</href>
			</Icon>
			<hotSpot x="20" y="2" xunits="pixels" yunits="pixels"/>
		</IconStyle>
		<LabelStyle>
			<scale>0.8</scale>
		</LabelStyle>
	</Style>

	<Folder>
		<name>Solicitud</name>
		<open>1</open>
{todas_las_rutas}
	</Folder>
</Document>
</kml>"""

    # ── Empaquetar el KML dentro de un KMZ (que es un ZIP) ────
    buffer_kmz = BytesIO()
    with zipfile.ZipFile(buffer_kmz, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("doc.kml", kml_completo.encode("utf-8"))

    return buffer_kmz.getvalue()


# ─────────────────────────────────────────────────────────────
# INTERFAZ DE USUARIO (STREAMLIT)
# ─────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Creación de KMZ del SEG",
    page_icon="🗺️",
    layout="wide",
)

# ── Encabezado ────────────────────────────────────────────────
st.title("🗺️ Creación de KMZ del SEG")
st.caption("CFE · Sistema Electrónico de Gestión")
st.markdown(
    "Sube tu archivo KMZ, selecciona qué elementos van en cada Ruta "
    "y descarga el KMZ listo para cargar al SEG."
)
st.divider()


# ── PASO 1: Subir archivo ─────────────────────────────────────
st.subheader("📁 Paso 1 — Sube tu archivo KMZ")

archivo = st.file_uploader(
    "Arrastra o selecciona tu archivo KMZ",
    type=["kmz"],
    help="El archivo puede tener cualquier estructura interna; la app lo reorganizará.",
)

# Solo continuar si el usuario subió un archivo
if archivo:
    bytes_archivo = archivo.read()

    # Intentar parsear el KMZ
    try:
        placemarks = parse_kmz(bytes_archivo)
    except Exception as error:
        st.error(f"❌ Error al leer el archivo: {error}")
        st.stop()

    # Separar por tipo
    lineas = [pm for pm in placemarks if pm["tipo"] == "Línea"]
    puntos = [pm for pm in placemarks if pm["tipo"] == "Punto"]
    otros  = [pm for pm in placemarks if pm["tipo"] == "Otro"]

    # Crear un diccionario por ID para acceso rápido
    placemarks_por_id = {pm["id"]: pm for pm in placemarks}

    # Mostrar resumen de lo encontrado
    st.success(f"✅ Archivo cargado: **{len(placemarks)}** elementos encontrados")

    col1, col2, col3 = st.columns(3)
    col1.metric("📏 Líneas (trayectorias)", len(lineas))
    col2.metric("📍 Puntos (postes)", len(puntos))
    col3.metric("❓ Otros", len(otros))

    # Tabla expandible con el detalle de todos los elementos
    with st.expander("🔍 Ver todos los elementos encontrados"):
        for pm in placemarks:
            icono = "📏" if pm["tipo"] == "Línea" else "📍" if pm["tipo"] == "Punto" else "❓"
            st.write(f"{icono} **{pm['name']}** · {pm['tipo']} · _Carpeta: {pm['path']}_")

    st.divider()

    # ── PASO 2: Configurar estructura ─────────────────────────
    st.subheader("⚙️ Paso 2 — Configura la estructura del SEG")

    nombre_proyecto = st.text_input(
        "Nombre del proyecto (sin símbolos ni comas)",
        value=archivo.name.replace(".kmz", ""),
        help="Este nombre aparecerá en el archivo KMZ generado.",
    )

    num_rutas = st.number_input(
        "¿Cuántas rutas tiene el proyecto?",
        min_value=1,
        max_value=10,
        value=1,
        step=1,
        help="Por cada ruta necesitas seleccionar sus trayectorias y postes.",
    )

    # Preparar las opciones para los multiselect
    opciones_lineas = [pm["id"] for pm in lineas]
    opciones_puntos = [pm["id"] for pm in puntos]

    def etiqueta_elemento(pm_id):
        """Genera la etiqueta legible para un elemento en los selectbox."""
        pm = placemarks_por_id.get(pm_id)
        if pm is None:
            return str(pm_id)
        return f"{pm['name']}   (📁 {pm['path']})"

    # Construir la asignación de elementos por Ruta
    rutas_config = {}

    for num_ruta in range(1, int(num_rutas) + 1):
        st.markdown(f"#### 📂 Ruta {num_ruta}")

        col_tray, col_post = st.columns(2)

        with col_tray:
            st.markdown("**Trayectoria** — líneas")
            trayectorias_sel = st.multiselect(
                f"Líneas de Ruta {num_ruta}",
                options=opciones_lineas,
                format_func=etiqueta_elemento,
                key=f"tray_{num_ruta}",
                label_visibility="collapsed",
                placeholder="Selecciona una o más líneas...",
            )

        with col_post:
            st.markdown("**Postes** — puntos")
            postes_sel = st.multiselect(
                f"Postes de Ruta {num_ruta}",
                options=opciones_puntos,
                format_func=etiqueta_elemento,
                key=f"postes_{num_ruta}",
                label_visibility="collapsed",
                placeholder="Selecciona uno o más puntos...",
            )

        rutas_config[num_ruta] = {
            "trayectoria": trayectorias_sel,
            "postes": postes_sel,
        }

    st.divider()

    # ── PASO 3: Generar y descargar ───────────────────────────
    st.subheader("⬇️ Paso 3 — Genera y descarga el KMZ")

    if st.button("🔄 Generar KMZ para el SEG", type="primary", use_container_width=True):
        # Validar que hay al menos un elemento asignado
        total_asignados = sum(
            len(r["trayectoria"]) + len(r["postes"])
            for r in rutas_config.values()
        )

        if total_asignados == 0:
            st.warning("⚠️ Asigna al menos un elemento (línea o punto) antes de generar.")
        elif not nombre_proyecto.strip():
            st.warning("⚠️ Escribe el nombre del proyecto antes de generar.")
        else:
            with st.spinner("Generando KMZ..."):
                kmz_bytes = generar_kmz(rutas_config, placemarks_por_id, nombre_proyecto.strip())

            # Guardar en session_state para que persista al hacer clic en descargar
            st.session_state["kmz_bytes"]  = kmz_bytes
            st.session_state["kmz_nombre"] = f"{nombre_proyecto.strip()}_SEG.kmz"
            st.success("✅ ¡KMZ generado correctamente!")

    # Mostrar botón de descarga si ya se generó el KMZ
    if "kmz_bytes" in st.session_state:
        st.download_button(
            label="⬇️ Descargar KMZ del SEG",
            data=st.session_state["kmz_bytes"],
            file_name=st.session_state.get("kmz_nombre", "resultado_SEG.kmz"),
            mime="application/vnd.google-earth.kmz",
            use_container_width=True,
        )

else:
    # Mostrar instrucciones cuando no hay archivo subido
    st.info(
        "👆 Sube un archivo KMZ para comenzar.\n\n"
        "La app detectará automáticamente las líneas (trayectorias) y "
        "los puntos (postes), y tú decides cómo organizarlos en la estructura del SEG."
    )
