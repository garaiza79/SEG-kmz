"""
Creación de KMZ del SEG - CFE  (v3.0)
═══════════════════════════════════════
Convierte un KMZ de proyecto al formato requerido por el SEG.
Con intersección espacial opcional para detectar Entidad y Municipio.
Paso 3 usa mapa Folium interactivo para seleccionar elementos visualmente.

Estructura de salida:
  Sin municipios:  Solicitud → Ruta N → Trayectoria / Postes
  Con municipios:  Entidad → Municipio → Solicitud → Ruta N → Trayectoria / Postes
"""

import streamlit as st
import zipfile
import xml.etree.ElementTree as ET
from io import BytesIO
import re
from collections import defaultdict

# ─── Registrar namespaces KML ────────────────────────────────
ET.register_namespace("",     "http://www.opengis.net/kml/2.2")
ET.register_namespace("gx",   "http://www.google.com/kml/ext/2.2")
ET.register_namespace("atom", "http://www.w3.org/2005/Atom")

KML = "http://www.opengis.net/kml/2.2"
GX  = "http://www.google.com/kml/ext/2.2"

# ─── Verificar si shapely está instalado ─────────────────────
try:
    from shapely.geometry import Point, LineString, Polygon
    from shapely.ops import unary_union
    from shapely.strtree import STRtree
    SHAPELY = True
except ImportError:
    SHAPELY = False

# ─── Verificar si folium está instalado ──────────────────────
try:
    import folium
    from streamlit_folium import st_folium
    FOLIUM = True
except ImportError:
    FOLIUM = False


# ══════════════════════════════════════════════════════════════
# UTILIDADES
# ══════════════════════════════════════════════════════════════

def coords_str_to_list(coords_str):
    """
    Convierte un string de coordenadas KML a lista de tuplas (lon, lat).
    Ejemplo: "-100.1,22.5,0 -100.2,22.6,0" → [(-100.1, 22.5), (-100.2, 22.6)]
    """
    coords = []
    if not coords_str:
        return coords
    for par in coords_str.strip().split():
        partes = par.split(",")
        if len(partes) >= 2:
            try:
                coords.append((float(partes[0]), float(partes[1])))
            except ValueError:
                pass
    return coords


# ─── Estilos inline que se inyectan directamente en cada Placemark ───────────
_ESTILO_LINEA = (
    "<Style>"
    "<LineStyle><color>ff0000ff</color><width>3</width></LineStyle>"
    "<LabelStyle><scale>0</scale></LabelStyle>"
    "</Style>"
)

_ESTILO_PUNTO = (
    "<Style>"
    "<IconStyle>"
    "<Icon><href>http://maps.google.com/mapfiles/kml/pushpin/ylw-pushpin.png</href></Icon>"
    "<hotSpot x='20' y='2' xunits='pixels' yunits='pixels'/>"
    "</IconStyle>"
    "<LabelStyle><scale>0.8</scale></LabelStyle>"
    "</Style>"
)


def elemento_a_string(elemento):
    """
    Convierte un Placemark a string KML limpio con estilo inline inyectado.
    """
    texto = ET.tostring(elemento, encoding="unicode")

    # ── 1. Limpiar prefijos de namespace ──────────────────────
    texto = re.sub(r' xmlns(?::\w+)?="[^"]*"', "", texto)
    texto = re.sub(r"<kml:", "<", texto)
    texto = re.sub(r"</kml:", "</", texto)

    # ── 2. Quitar <description> con TODO su contenido ─────────
    texto = re.sub(r"<description\b[^>]*>.*?</description>", "", texto, flags=re.DOTALL)
    texto = re.sub(r"<description\s*/>", "", texto)

    # ── 2b. Quitar <ExtendedData> ──────────────────────────────
    texto = re.sub(r"<ExtendedData\b[^>]*>.*?</ExtendedData>", "", texto, flags=re.DOTALL)
    texto = re.sub(r"<ExtendedData\s*/>", "", texto)

    # ── 3. Quitar <styleUrl> y <Style> del Placemark original ─
    texto = re.sub(r"<styleUrl\b[^>]*>.*?</styleUrl>", "", texto, flags=re.DOTALL)
    texto = re.sub(r"<Style\b[^>]*>.*?</Style>",       "", texto, flags=re.DOTALL)
    texto = re.sub(r"<StyleMap\b[^>]*>.*?</StyleMap>", "", texto, flags=re.DOTALL)

    # ── 4. Inyectar estilo inline después del tag <Placemark...> ──
    if "<LineString>" in texto:
        estilo_inline = _ESTILO_LINEA
    else:
        estilo_inline = _ESTILO_PUNTO

    texto = re.sub(
        r"(<Placemark\b[^>]*>)",
        lambda m: m.group(1) + estilo_inline,
        texto,
        count=1,
    )

    return texto


# ══════════════════════════════════════════════════════════════
# FUNCIÓN 1: PARSEAR KMZ DEL PROYECTO
# ══════════════════════════════════════════════════════════════

def parse_kmz_proyecto(file_bytes):
    """
    Lee el KMZ del proyecto y extrae todos los Placemarks (puntos y líneas).
    """
    with zipfile.ZipFile(BytesIO(file_bytes)) as z:
        kml_file = next((n for n in z.namelist() if n.endswith(".kml")), None)
        if not kml_file:
            raise ValueError("No se encontró archivo .kml dentro del KMZ.")
        kml_content = z.read(kml_file).decode("utf-8")

    root     = ET.fromstring(kml_content)
    document = root.find(f"{{{KML}}}Document") or root

    placemarks = []
    contador   = [0]

    def extraer(elemento, ruta=""):
        for hijo in elemento:
            tag = hijo.tag.replace(f"{{{KML}}}", "")

            if tag in ("Folder", "Document"):
                n = hijo.find(f"{{{KML}}}name")
                nombre_carpeta = n.text if n is not None else "Sin nombre"
                nueva_ruta = f"{ruta} / {nombre_carpeta}" if ruta else nombre_carpeta
                extraer(hijo, nueva_ruta)

            elif tag == "Placemark":
                n      = hijo.find(f"{{{KML}}}name")
                nombre = n.text if n is not None else "Sin nombre"

                coords_el = hijo.find(f".//{{{KML}}}coordinates")
                coords    = coords_str_to_list(coords_el.text) if coords_el is not None else []

                if hijo.find(f"{{{KML}}}LineString") is not None:
                    tipo = "Línea"
                elif hijo.find(f"{{{KML}}}Point") is not None:
                    tipo = "Punto"
                else:
                    tipo = "Otro"

                contador[0] += 1
                placemarks.append({
                    "id":        contador[0],
                    "name":      nombre,
                    "tipo":      tipo,
                    "path":      ruta,
                    "coords":    coords,
                    "element":   hijo,
                    "municipio": None,
                })

    extraer(document)
    return placemarks


# ══════════════════════════════════════════════════════════════
# FUNCIÓN 2: PARSEAR KMZ DE MUNICIPIOS
# ══════════════════════════════════════════════════════════════

@st.cache_data(show_spinner="Procesando municipios, un momento...")
def parse_kmz_municipios(file_bytes):
    """
    Lee el KMZ de municipios (INEGI/CONABIO) y extrae los polígonos.
    """
    with zipfile.ZipFile(BytesIO(file_bytes)) as z:
        kml_file    = next((n for n in z.namelist() if n.endswith(".kml")), None)
        kml_content = z.read(kml_file).decode("utf-8")

    placemarks_raw = re.findall(
        r"<Placemark[^>]*>.*?</Placemark>", kml_content, re.DOTALL
    )

    municipios = []
    for pm in placemarks_raw:
        cve_ent = re.search(r"cve_ent.*?atr-value\">(.*?)</span>", pm, re.DOTALL)
        nom_ent = re.search(r"nom_ent.*?atr-value\">(.*?)</span>", pm, re.DOTALL)
        cve_mun = re.search(r"cve_mun.*?atr-value\">(.*?)</span>", pm, re.DOTALL)
        nom_mun = re.search(r"nom_mun.*?atr-value\">(.*?)</span>", pm, re.DOTALL)

        if not (nom_ent and nom_mun):
            continue

        poligonos_coords = []
        for poly_str in re.findall(r"<Polygon>.*?</Polygon>", pm, re.DOTALL):
            outer = re.search(
                r"<outerBoundaryIs>.*?<coordinates>(.*?)</coordinates>",
                poly_str, re.DOTALL
            )
            if outer:
                coords = coords_str_to_list(outer.group(1))
                if len(coords) >= 3:
                    poligonos_coords.append(coords)

        if poligonos_coords:
            municipios.append({
                "cve_ent":   cve_ent.group(1).strip() if cve_ent else "",
                "nom_ent":   nom_ent.group(1).strip(),
                "cve_mun":   cve_mun.group(1).strip() if cve_mun else "",
                "nom_mun":   nom_mun.group(1).strip(),
                "poligonos": poligonos_coords,
            })

    return municipios


# ══════════════════════════════════════════════════════════════
# FUNCIÓN 3: CALCULAR INTERSECCIÓN ESPACIAL
# ══════════════════════════════════════════════════════════════

def calcular_interseccion(placemarks, municipios_data):
    """
    Para cada Placemark detecta en qué municipio se encuentra.
    """
    if not SHAPELY:
        return placemarks

    geoms_shapely = []
    for mun in municipios_data:
        polys = []
        for coords in mun["poligonos"]:
            try:
                polys.append(Polygon(coords))
            except Exception:
                pass
        geom = unary_union(polys) if polys else None
        geoms_shapely.append(geom)

    pares_validos  = [(i, g) for i, g in enumerate(geoms_shapely) if g is not None]
    indices_reales = [i for i, _ in pares_validos]
    geoms_validas  = [g for _, g in pares_validos]

    tree = STRtree(geoms_validas)

    for pm in placemarks:
        if not pm["coords"]:
            continue
        try:
            if pm["tipo"] == "Punto":
                sg        = Point(pm["coords"][0])
                candidatos = list(tree.query(sg))
                mejor = None
                for ic in candidatos:
                    ir = indices_reales[ic]
                    if geoms_shapely[ir].contains(sg) or geoms_shapely[ir].distance(sg) < 0.001:
                        mejor = municipios_data[ir]
                        break
                if mejor is None and candidatos:
                    d_min = float("inf")
                    for ic in candidatos:
                        ir = indices_reales[ic]
                        d  = geoms_shapely[ir].distance(sg)
                        if d < d_min:
                            d_min = d
                            mejor = municipios_data[ir]
                pm["municipio"] = mejor

            elif pm["tipo"] == "Línea" and len(pm["coords"]) >= 2:
                sg         = LineString(pm["coords"])
                candidatos = list(tree.query(sg))
                mejor      = None
                mejor_lon  = 0.0
                for ic in candidatos:
                    ir = indices_reales[ic]
                    try:
                        inter = sg.intersection(geoms_shapely[ir])
                        if not inter.is_empty and inter.length > mejor_lon:
                            mejor_lon = inter.length
                            mejor     = municipios_data[ir]
                    except Exception:
                        pass
                pm["municipio"] = mejor

        except Exception:
            pm["municipio"] = None

    return placemarks


# ══════════════════════════════════════════════════════════════
# ESTILOS KML DE SALIDA
# ══════════════════════════════════════════════════════════════

ESTILOS_KML = """
\t<Style id="estilo_trayectoria">
\t\t<IconStyle><scale>0</scale><Icon><href>http://maps.google.com/mapfiles/kml/pushpin/ylw-pushpin.png</href></Icon></IconStyle>
\t\t<LabelStyle><scale>0</scale></LabelStyle>
\t\t<LineStyle><color>ff0000ff</color><width>3</width></LineStyle>
\t</Style>
\t<Style id="estilo_poste">
\t\t<IconStyle><Icon><href>http://maps.google.com/mapfiles/kml/pushpin/ylw-pushpin.png</href></Icon><hotSpot x="20" y="2" xunits="pixels" yunits="pixels"/></IconStyle>
\t\t<LabelStyle><scale>0.8</scale></LabelStyle>
\t</Style>"""


# ══════════════════════════════════════════════════════════════
# FUNCIÓN 4: GENERAR KMZ DE SALIDA
# ══════════════════════════════════════════════════════════════

def xml_bloque_solicitud(rutas_config, placemarks_por_id, sangria="\t"):
    """Construye el bloque XML Solicitud → Ruta N → Trayectoria / Postes."""
    s = sangria
    xml_rutas = []

    for num_ruta, datos in rutas_config.items():
        xml_tray = ""
        for pm_id in datos["trayectoria"]:
            if pm_id in placemarks_por_id:
                xml_tray += s + "\t\t\t" + elemento_a_string(placemarks_por_id[pm_id]["element"]) + "\n"

        xml_post = ""
        for pm_id in datos["postes"]:
            if pm_id in placemarks_por_id:
                xml_post += s + "\t\t\t" + elemento_a_string(placemarks_por_id[pm_id]["element"]) + "\n"

        xml_rutas.append(
            f"{s}\t<Folder>\n"
            f"{s}\t\t<name>Ruta {num_ruta}</name>\n"
            f"{s}\t\t<open>1</open>\n"
            f"{s}\t\t<Folder>\n"
            f"{s}\t\t\t<name>Trayectoria</name>\n"
            f"{xml_tray}"
            f"{s}\t\t</Folder>\n"
            f"{s}\t\t<Folder>\n"
            f"{s}\t\t\t<name>Postes</name>\n"
            f"{xml_post}"
            f"{s}\t\t</Folder>\n"
            f"{s}\t</Folder>\n"
        )

    return (
        f"{s}<Folder>\n"
        f"{s}\t<name>Solicitud</name>\n"
        f"{s}\t<open>1</open>\n"
        + "".join(xml_rutas)
        + f"{s}</Folder>\n"
    )


def generar_kmz(rutas_config, placemarks_por_id, nombre_proyecto, con_municipio=False):
    """
    Genera el KMZ final con la estructura del SEG.
    """
    if not con_municipio:
        cuerpo = xml_bloque_solicitud(rutas_config, placemarks_por_id, "\t")

    else:
        elems_por_mun = defaultdict(lambda: defaultdict(lambda: {"trayectoria": [], "postes": []}))

        for num_ruta, datos in rutas_config.items():
            for pm_id in datos["trayectoria"]:
                if pm_id in placemarks_por_id:
                    mun = placemarks_por_id[pm_id].get("municipio")
                    if mun:
                        clave = (mun["cve_ent"], mun["nom_ent"], mun["cve_mun"], mun["nom_mun"])
                        elems_por_mun[clave][num_ruta]["trayectoria"].append(pm_id)
            for pm_id in datos["postes"]:
                if pm_id in placemarks_por_id:
                    mun = placemarks_por_id[pm_id].get("municipio")
                    if mun:
                        clave = (mun["cve_ent"], mun["nom_ent"], mun["cve_mun"], mun["nom_mun"])
                        elems_por_mun[clave][num_ruta]["postes"].append(pm_id)

        if not elems_por_mun:
            cuerpo = xml_bloque_solicitud(rutas_config, placemarks_por_id, "\t")
        else:
            entidades = defaultdict(list)
            for (cve_ent, nom_ent, cve_mun, nom_mun), rutas_mun in sorted(elems_por_mun.items()):
                solicitud_xml  = xml_bloque_solicitud(rutas_mun, placemarks_por_id, "\t\t\t")
                municipio_xml  = (
                    f"\t\t<Folder>\n"
                    f"\t\t\t<name>{cve_mun} - {nom_mun}</name>\n"
                    f"\t\t\t<open>1</open>\n"
                    f"{solicitud_xml}"
                    f"\t\t</Folder>\n"
                )
                entidades[(cve_ent, nom_ent)].append(municipio_xml)

            cuerpo = ""
            for (cve_ent, nom_ent), municipios_xml in sorted(entidades.items()):
                cuerpo += (
                    f"\t<Folder>\n"
                    f"\t\t<name>{cve_ent} - {nom_ent}</name>\n"
                    f"\t\t<open>1</open>\n"
                    + "".join(municipios_xml)
                    + f"\t</Folder>\n"
                )

    kml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<kml xmlns="http://www.opengis.net/kml/2.2"\n'
        '     xmlns:gx="http://www.google.com/kml/ext/2.2"\n'
        '     xmlns:kml="http://www.opengis.net/kml/2.2"\n'
        '     xmlns:atom="http://www.w3.org/2005/Atom">\n'
        '<Document>\n'
        f'\t<name>{nombre_proyecto}</name>\n'
        f'{ESTILOS_KML}\n'
        f'{cuerpo}'
        '</Document>\n'
        '</kml>'
    )

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("doc.kml", kml.encode("utf-8"))
    return buffer.getvalue()


# ══════════════════════════════════════════════════════════════
# MAPA INTERACTIVO (PASO 3)
# ══════════════════════════════════════════════════════════════

# Colores para cada ruta en el mapa (formato HTML hex)
COLORES_RUTA = [
    "#2ecc71",  # verde
    "#e74c3c",  # rojo
    "#3498db",  # azul
    "#f39c12",  # naranja
    "#9b59b6",  # morado
    "#1abc9c",  # turquesa
    "#e67e22",  # naranja oscuro
    "#34495e",  # gris oscuro
    "#e91e63",  # rosa
    "#00bcd4",  # cyan
]


def _color_elemento(pm_id, selections):
    """Devuelve (color_hex, etiqueta_asignacion) para un elemento."""
    for ruta_num, datos in selections.items():
        if pm_id in datos.get("trayectoria", set()):
            color = COLORES_RUTA[(ruta_num - 1) % len(COLORES_RUTA)]
            return color, f"Ruta {ruta_num} · Trayectoria"
        if pm_id in datos.get("postes", set()):
            color = COLORES_RUTA[(ruta_num - 1) % len(COLORES_RUTA)]
            return color, f"Ruta {ruta_num} · Postes"
    return "#888888", "Sin asignar"


def construir_mapa(placemarks, selections):
    """
    Construye el mapa Folium con todos los elementos coloreados según su asignación.
    Gris = sin asignar · Color = asignado a una ruta
    """
    # Calcular centro
    all_coords = []
    for pm in placemarks:
        if pm["coords"]:
            all_coords.extend(pm["coords"])

    if all_coords:
        center_lat = sum(c[1] for c in all_coords) / len(all_coords)
        center_lon = sum(c[0] for c in all_coords) / len(all_coords)
    else:
        center_lat, center_lon = 23.0, -102.0

    m = folium.Map(
        location=[center_lat, center_lon],
        zoom_start=13,
        tiles="OpenStreetMap",
    )

    for pm in placemarks:
        if not pm["coords"]:
            continue

        color, asign_label = _color_elemento(pm["id"], selections)
        # Popup con ID parseable + nombre + etiqueta
        popup_txt = f"ID:{pm['id']}|{pm['name']}|{asign_label}"
        tooltip_txt = f"{pm['name']}\n{asign_label}"

        if pm["tipo"] == "Línea" and len(pm["coords"]) >= 2:
            # Dibujar la línea
            folium.PolyLine(
                locations=[(c[1], c[0]) for c in pm["coords"]],
                color=color,
                weight=4,
                opacity=0.85,
                tooltip=folium.Tooltip(tooltip_txt),
                popup=folium.Popup(popup_txt, max_width=300),
            ).add_to(m)
            # Marcador en punto medio para facilitar clic
            mid = pm["coords"][len(pm["coords"]) // 2]
            folium.CircleMarker(
                location=[mid[1], mid[0]],
                radius=7,
                color=color,
                fill=True,
                fill_color=color,
                fill_opacity=0.9,
                tooltip=folium.Tooltip(tooltip_txt),
                popup=folium.Popup(popup_txt, max_width=300),
            ).add_to(m)

        elif pm["tipo"] == "Punto":
            coord = pm["coords"][0]
            folium.CircleMarker(
                location=[coord[1], coord[0]],
                radius=8,
                color=color,
                fill=True,
                fill_color=color,
                fill_opacity=0.9,
                tooltip=folium.Tooltip(tooltip_txt),
                popup=folium.Popup(popup_txt, max_width=300),
            ).add_to(m)

    return m


# ══════════════════════════════════════════════════════════════
# INTERFAZ STREAMLIT
# ══════════════════════════════════════════════════════════════

st.set_page_config(
    page_title="Creación de KMZ del SEG",
    page_icon="🗺️",
    layout="wide",
)

st.title("🗺️ Creación de KMZ del SEG")
st.caption("CFE · Sistema Electrónico de Gestión")
st.divider()

# ── PASO 1: KMZ del proyecto ──────────────────────────────────
st.subheader("📁 Paso 1 — KMZ del proyecto (líneas y postes)")
archivo_proyecto = st.file_uploader(
    "Sube el KMZ con tus trayectorias y postes",
    type=["kmz"],
    key="proyecto",
    help="Puede tener cualquier estructura interna.",
)

st.divider()

# ── PASO 2: KMZ de municipios (opcional) ─────────────────────
st.subheader("🗺️ Paso 2 — KMZ de Entidades/Municipios (opcional)")

col_txt, col_up = st.columns([3, 1])
with col_txt:
    st.info(
        "Si subes el KMZ de municipios (INEGI/CONABIO), la app detecta automáticamente "
        "en qué **Entidad y Municipio** cae cada línea y poste, y genera la estructura:\n\n"
        "**Entidad → Municipio → Solicitud → Ruta → Trayectoria / Postes**"
    )
with col_up:
    archivo_municipios = st.file_uploader(
        "KMZ de Municipios",
        type=["kmz"],
        key="municipios",
        label_visibility="collapsed",
    )

st.divider()

# ── Procesamiento ─────────────────────────────────────────────
if not archivo_proyecto:
    st.info("👆 Sube tu KMZ del proyecto en el Paso 1 para comenzar.")
    st.stop()

bytes_proyecto = archivo_proyecto.read()

# Detectar si cambió el archivo y resetear selecciones
import hashlib
kmz_hash = hashlib.md5(bytes_proyecto).hexdigest()
if st.session_state.get("kmz_hash") != kmz_hash:
    st.session_state["kmz_hash"]       = kmz_hash
    st.session_state["selections"]     = {}
    st.session_state["last_click_popup"] = None

try:
    placemarks = parse_kmz_proyecto(bytes_proyecto)
except Exception as e:
    st.error(f"❌ Error al leer el KMZ del proyecto: {e}")
    st.stop()

lineas = [pm for pm in placemarks if pm["tipo"] == "Línea"]
puntos = [pm for pm in placemarks if pm["tipo"] == "Punto"]
placemarks_por_id = {pm["id"]: pm for pm in placemarks}

st.success(f"✅ Proyecto cargado: **{len(placemarks)}** elementos")
c1, c2, c3 = st.columns(3)
c1.metric("📏 Líneas (trayectorias)", len(lineas))
c2.metric("📍 Puntos (postes)",        len(puntos))
c3.metric("❓ Otros",                  len(placemarks) - len(lineas) - len(puntos))

with st.expander("🔍 Ver todos los elementos del proyecto"):
    for pm in placemarks:
        icono = "📏" if pm["tipo"] == "Línea" else "📍" if pm["tipo"] == "Punto" else "❓"
        st.write(f"{icono} **{pm['name']}** · {pm['tipo']} · _📁 {pm['path']}_")

st.divider()

# ── Intersección espacial ─────────────────────────────────────
con_municipio = False

if archivo_municipios:
    if not SHAPELY:
        st.error("❌ La librería **shapely** no está instalada. Agrégala al `requirements.txt`.")
    else:
        bytes_municipios = archivo_municipios.read()
        try:
            municipios_data = parse_kmz_municipios(bytes_municipios)
            n_estados = len(set(m["cve_ent"] for m in municipios_data))
            st.success(
                f"✅ Municipios cargados: **{len(municipios_data):,}** municipios "
                f"en **{n_estados}** estados"
            )

            with st.spinner("Calculando intersección espacial..."):
                placemarks        = calcular_interseccion(placemarks, municipios_data)
                placemarks_por_id = {pm["id"]: pm for pm in placemarks}

            con_municipio = True

            st.subheader("📍 Municipios detectados por intersección")
            for pm in placemarks:
                mun   = pm["municipio"]
                icono = "📏" if pm["tipo"] == "Línea" else "📍"
                if mun:
                    st.write(
                        f"{icono} **{pm['name']}** → "
                        f"`{mun['cve_ent']}` **{mun['nom_ent']}** / "
                        f"`{mun['cve_mun']}` **{mun['nom_mun']}**"
                    )
                else:
                    st.warning(f"{icono} **{pm['name']}** → no se encontró municipio")

            st.divider()

        except Exception as e:
            st.error(f"❌ Error al procesar el KMZ de municipios: {e}")


# ══════════════════════════════════════════════════════════════
# ── PASO 3: Mapa interactivo para seleccionar elementos ───────
# ══════════════════════════════════════════════════════════════
st.subheader("⚙️ Paso 3 — Selecciona los elementos en el mapa")

# ─── Número de rutas ──────────────────────────────────────────
num_rutas = st.number_input(
    "¿Cuántas rutas tiene el proyecto?",
    min_value=1, max_value=10, value=1, step=1,
    key="num_rutas_input",
)
num_rutas = int(num_rutas)

# ─── Inicializar / sincronizar session_state ──────────────────
if "selections" not in st.session_state:
    st.session_state["selections"] = {}

for rn in range(1, num_rutas + 1):
    if rn not in st.session_state["selections"]:
        st.session_state["selections"][rn] = {"trayectoria": set(), "postes": set()}

# Limpiar rutas que ya no existen
for rn in list(st.session_state["selections"].keys()):
    if rn > num_rutas:
        del st.session_state["selections"][rn]

if "ruta_activa" not in st.session_state:
    st.session_state["ruta_activa"] = 1
if "tipo_activo" not in st.session_state:
    st.session_state["tipo_activo"] = "postes"
if "last_click_popup" not in st.session_state:
    st.session_state["last_click_popup"] = None

# Asegurar que ruta_activa esté dentro del rango
if st.session_state["ruta_activa"] > num_rutas:
    st.session_state["ruta_activa"] = 1

# ─── Layout: columna mapa | columna panel ─────────────────────
if not FOLIUM:
    st.error(
        "❌ Las librerías **folium** y **streamlit-folium** no están instaladas. "
        "Agrégalas al `requirements.txt`:\n```\nfolium\nstreamlit-folium\n```"
    )
    # Fallback: multiselect clásico
    opciones_lineas = [pm["id"] for pm in lineas]
    opciones_puntos = [pm["id"] for pm in puntos]

    def etiqueta_fallback(pm_id):
        pm = placemarks_por_id.get(pm_id, {})
        return f"{pm.get('name', pm_id)}   (📁 {pm.get('path', '')})"

    rutas_config_fallback = {}
    for num_ruta in range(1, num_rutas + 1):
        st.markdown(f"#### 📂 Ruta {num_ruta}")
        col_t, col_p = st.columns(2)
        with col_t:
            st.markdown("**Trayectoria** — líneas")
            sel_tray = st.multiselect(
                f"tray_{num_ruta}", options=opciones_lineas,
                format_func=etiqueta_fallback,
                key=f"tray_fb_{num_ruta}", label_visibility="collapsed",
            )
        with col_p:
            st.markdown("**Postes** — puntos")
            sel_postes = st.multiselect(
                f"postes_{num_ruta}", options=opciones_puntos,
                format_func=etiqueta_fallback,
                key=f"post_fb_{num_ruta}", label_visibility="collapsed",
            )
        rutas_config_fallback[num_ruta] = {"trayectoria": sel_tray, "postes": sel_postes}

    rutas_config = rutas_config_fallback

else:
    # ══ MAPA INTERACTIVO ══════════════════════════════════════
    col_mapa, col_panel = st.columns([3, 2])

    # ─── Panel derecho ────────────────────────────────────────
    with col_panel:
        st.markdown("### Panel de asignación")

        ruta_activa = st.selectbox(
            "Ruta activa",
            options=list(range(1, num_rutas + 1)),
            format_func=lambda x: f"Ruta {x}",
            index=min(st.session_state["ruta_activa"] - 1, num_rutas - 1),
            key="ruta_activa_select",
        )
        st.session_state["ruta_activa"] = ruta_activa

        tipo_activo = st.radio(
            "Tipo de elemento",
            options=["postes", "trayectoria"],
            format_func=lambda x: "📍 Postes" if x == "postes" else "📏 Trayectoria",
            index=0 if st.session_state["tipo_activo"] == "postes" else 1,
            key="tipo_activo_radio",
            horizontal=True,
        )
        st.session_state["tipo_activo"] = tipo_activo

        color_activo = COLORES_RUTA[(ruta_activa - 1) % len(COLORES_RUTA)]
        tipo_label   = "Postes" if tipo_activo == "postes" else "Trayectoria"
        st.markdown(
            f"<div style='background:{color_activo};padding:8px 12px;border-radius:6px;"
            f"color:white;font-weight:bold;text-align:center;margin:6px 0'>"
            f"➕ Clic en mapa → Ruta {ruta_activa} / {tipo_label}"
            f"</div>",
            unsafe_allow_html=True,
        )
        st.caption("Haz clic de nuevo en un elemento para quitarlo.")

        st.markdown("---")

        # Mostrar elementos asignados por ruta
        total_global = sum(
            len(d.get("trayectoria", set())) + len(d.get("postes", set()))
            for d in st.session_state["selections"].values()
        )
        st.markdown(f"**Total asignados: {total_global}**")

        for rn in range(1, num_rutas + 1):
            sel_rn   = st.session_state["selections"].get(rn, {})
            tray_ids = list(sel_rn.get("trayectoria", set()))
            post_ids = list(sel_rn.get("postes", set()))
            total_rn = len(tray_ids) + len(post_ids)
            color_rn = COLORES_RUTA[(rn - 1) % len(COLORES_RUTA)]

            header_html = (
                f"<span style='display:inline-block;width:12px;height:12px;"
                f"background:{color_rn};border-radius:50%;margin-right:6px'></span>"
                f"Ruta {rn} · {total_rn} elemento{'s' if total_rn != 1 else ''}"
            )

            with st.expander(f"Ruta {rn} · {total_rn} elementos", expanded=(rn == ruta_activa)):
                if tray_ids:
                    st.markdown("**📏 Trayectoria:**")
                    for pm_id in tray_ids:
                        pm = placemarks_por_id.get(pm_id, {})
                        c1, c2 = st.columns([5, 1])
                        c1.caption(pm.get("name", str(pm_id)))
                        if c2.button("✕", key=f"rm_tray_{rn}_{pm_id}", help="Quitar"):
                            st.session_state["selections"][rn]["trayectoria"].discard(pm_id)
                            st.rerun()

                if post_ids:
                    st.markdown("**📍 Postes:**")
                    for pm_id in post_ids:
                        pm = placemarks_por_id.get(pm_id, {})
                        c1, c2 = st.columns([5, 1])
                        c1.caption(pm.get("name", str(pm_id)))
                        if c2.button("✕", key=f"rm_post_{rn}_{pm_id}", help="Quitar"):
                            st.session_state["selections"][rn]["postes"].discard(pm_id)
                            st.rerun()

                if not tray_ids and not post_ids:
                    st.caption("Sin elementos asignados.")

    # ─── Mapa izquierdo ───────────────────────────────────────
    with col_mapa:
        mapa = construir_mapa(placemarks, st.session_state["selections"])
        resultado_mapa = st_folium(
            mapa,
            width=700,
            height=520,
            returned_objects=["last_object_clicked_popup"],
            key="mapa_principal",
        )

        # ── Procesar clic en el mapa ──────────────────────────
        popup_click = (
            resultado_mapa.get("last_object_clicked_popup")
            if resultado_mapa else None
        )

        if (
            popup_click
            and isinstance(popup_click, str)
            and popup_click != st.session_state.get("last_click_popup")
        ):
            st.session_state["last_click_popup"] = popup_click

            if popup_click.startswith("ID:"):
                partes = popup_click.split("|")
                try:
                    pm_id    = int(partes[0].replace("ID:", "").strip())
                    ruta_num = st.session_state["ruta_activa"]
                    tipo     = st.session_state["tipo_activo"]

                    if ruta_num not in st.session_state["selections"]:
                        st.session_state["selections"][ruta_num] = {
                            "trayectoria": set(), "postes": set()
                        }

                    sel_set = st.session_state["selections"][ruta_num][tipo]

                    if pm_id in sel_set:
                        # Toggle: quitar
                        sel_set.discard(pm_id)
                        st.toast(f"✕ Quitado de Ruta {ruta_num} / {tipo_label}")
                    else:
                        # Quitar de cualquier asignación anterior
                        for otras in st.session_state["selections"].values():
                            otras["trayectoria"].discard(pm_id)
                            otras["postes"].discard(pm_id)
                        # Asignar
                        sel_set.add(pm_id)
                        st.toast(f"✓ Asignado a Ruta {ruta_num} / {tipo_label}")

                    st.rerun()

                except (ValueError, IndexError):
                    pass

        # Leyenda de colores
        leyenda_items = []
        for rn in range(1, num_rutas + 1):
            c = COLORES_RUTA[(rn - 1) % len(COLORES_RUTA)]
            leyenda_items.append(
                f"<span style='display:inline-flex;align-items:center;margin-right:12px'>"
                f"<span style='display:inline-block;width:14px;height:14px;background:{c};"
                f"border-radius:50%;margin-right:4px'></span>Ruta {rn}</span>"
            )
        leyenda_items.append(
            "<span style='display:inline-flex;align-items:center'>"
            "<span style='display:inline-block;width:14px;height:14px;background:#888888;"
            "border-radius:50%;margin-right:4px'></span>Sin asignar</span>"
        )
        st.markdown(
            "<div style='margin-top:6px;font-size:0.82em'>" + "".join(leyenda_items) + "</div>",
            unsafe_allow_html=True,
        )

    # Construir rutas_config desde session_state
    rutas_config = {}
    for rn in range(1, num_rutas + 1):
        sel = st.session_state["selections"].get(rn, {})
        rutas_config[rn] = {
            "trayectoria": list(sel.get("trayectoria", set())),
            "postes":      list(sel.get("postes", set())),
        }


st.divider()

# ── PASO 4: Nombre del proyecto ───────────────────────────────
st.subheader("⬇️ Paso 4 — Genera y descarga el KMZ")

nombre_proyecto = st.text_input(
    "Nombre del proyecto (sin símbolos ni comas)",
    value=archivo_proyecto.name.replace(".kmz", ""),
)

if con_municipio:
    st.info("📐 Estructura: **Entidad → Municipio → Solicitud → Ruta → Trayectoria / Postes**")
else:
    st.info("📐 Estructura: **Solicitud → Ruta → Trayectoria / Postes**  _(sin municipios)_")

if st.button("🔄 Generar KMZ para el SEG", type="primary", use_container_width=True):
    total = sum(len(r["trayectoria"]) + len(r["postes"]) for r in rutas_config.values())
    if total == 0:
        st.warning("⚠️ Asigna al menos un elemento en el mapa antes de generar.")
    elif not nombre_proyecto.strip():
        st.warning("⚠️ Escribe el nombre del proyecto.")
    else:
        with st.spinner("Generando KMZ..."):
            kmz_bytes = generar_kmz(
                rutas_config,
                placemarks_por_id,
                nombre_proyecto.strip(),
                con_municipio,
            )
        st.session_state["kmz_bytes"]  = kmz_bytes
        st.session_state["kmz_nombre"] = f"{nombre_proyecto.strip()}_SEG.kmz"
        st.success("✅ ¡KMZ generado correctamente!")

if "kmz_bytes" in st.session_state:
    st.download_button(
        label="⬇️ Descargar KMZ del SEG",
        data=st.session_state["kmz_bytes"],
        file_name=st.session_state.get("kmz_nombre", "resultado_SEG.kmz"),
        mime="application/vnd.google-earth.kmz",
        use_container_width=True,
    )
