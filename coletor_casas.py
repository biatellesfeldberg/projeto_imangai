"""
coletor_casas.py
----------------
Coleta links de anúncios na web, extrai dados de páginas de imóveis e mantém apenas
anúncios que passam pelo filtro geográfico (`filtro_regioes.FiltroRegioes`).

Dependências:
    pip install requests beautifulsoup4 geopy

Uso típico:
    python coletor_casas.py --config config_coleta.exemplo.json

Observações:
    - Sites grandes costumam usar Cloudflare e podem bloquear IPs automatizados; em ambiente
      resid/com cookies de navegador o `requests` pode funcionar. Caso contrário, adapte o fetch
      (ex.: Playwright) mantendo a mesma interface de parsing.
    - O filtro espacial é estrito: sem coordenadas confiáveis na página nem geocodificação
      bem-sucedida dentro do polígono, o anúncio é descartado.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qs, unquote, urlparse

import requests
from bs4 import BeautifulSoup
from geopy.exc import GeocoderServiceError, GeocoderTimedOut
from geopy.geocoders import Nominatim

from filtro_regioes import (
    BoundingBox,
    FiltroRegioes,
    extrair_regioes_da_pasta,
    ponto_em_poligono,
)

LOG = logging.getLogger(__name__)

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Palavras que indicam que não é casa isolada / sobrado
PALAVRAS_EXCLUIR_TIPO = (
    "apartamento",
    "apto",
    "cobertura",
    "terreno",
    "lote",
    "galpão",
    "galpao",
    "sala comercial",
    "ponto comercial",
    "kitnet",
    "quitinete",
    "studio",
    "stúdio",
    "flat",
    "loft",
    "sala ",
    "loja",
)

PALAVRAS_PREFERIR_CASA = (
    "casa",
    "sobrado",
    "geminada",
    "condomínio fechado",
    "condominio fechado",
    "casa de vila",
)

DOMINIOS_PADRAO_PORTAIS = [
    "vivareal.com.br",
    "zapimoveis.com.br",
    "olx.com.br",
    "imovelweb.com.br",
    "chavesnamao.com.br",
    "mercadolivre.com.br",
]

RE_TELEFONE_BR = re.compile(
    r"(?:\+?55\s*)?"
    r"(?:\(\s*\d{2}\s*\)|\d{2})\s*"
    r"\d{4,5}\s*[-.\s]?\s*\d{4}\b"
)

RE_M2 = re.compile(
    r"(\d{1,3}(?:\.\d{3})*|\d+)\s*(?:m²|m2|metros?\s*quadrados?)",
    re.IGNORECASE,
)


@dataclass
class AnuncioCasa:
    endereco: str
    tamanho_m2: str
    link: str
    telefone_imobiliaria: str
    telefone_vendedor: str
    fonte: str
    regiao_poligono_id: str = ""
    latitude: str = ""
    longitude: str = ""
    titulo: str = ""
    motivo_exclusao: str = ""

    def linha_csv(self) -> dict[str, str]:
        return asdict(self)


@dataclass
class ConfigColeta:
    bounding_box: BoundingBox
    bounding_boxes_por_imagem: dict[str, BoundingBox] | None = None
    arquivo_saida_csv: str = "saida/casas_filtradas.csv"
    filtro_json: str | None = None
    regenerar_filtro_das_imagens: bool = True
    pasta_regioes_interesse: str = "regioes_interesse"
    queries_busca: list[str] = field(default_factory=list)
    max_links_por_query: int = 12
    google_cse_api_key: str | None = None
    google_cse_cx: str | None = None
    user_agent: str = DEFAULT_UA
    pausa_segundos_entre_requisicoes: float = 2.0
    dominios_permitidos: list[str] = field(default_factory=lambda: list(DOMINIOS_PADRAO_PORTAIS))
    idioma_busca_ddg: str = "br-pt"
    timeout_http: int = 35

    @staticmethod
    def from_dict(data: dict[str, Any]) -> ConfigColeta:
        bb = data["bounding_box"]
        bbox = BoundingBox(
            sul=float(bb["sul"]),
            oeste=float(bb["oeste"]),
            norte=float(bb["norte"]),
            leste=float(bb["leste"]),
        )
        por_img: dict[str, BoundingBox] | None = None
        raw_map = data.get("bounding_boxes_por_imagem")
        if isinstance(raw_map, dict) and raw_map:
            por_img = {}
            for nome_arq, bb_raw in raw_map.items():
                if not isinstance(bb_raw, dict):
                    continue
                por_img[str(nome_arq)] = BoundingBox(
                    sul=float(bb_raw["sul"]),
                    oeste=float(bb_raw["oeste"]),
                    norte=float(bb_raw["norte"]),
                    leste=float(bb_raw["leste"]),
                )
        return ConfigColeta(
            bounding_box=bbox,
            bounding_boxes_por_imagem=por_img,
            arquivo_saida_csv=str(
                data.get("arquivo_saida_csv", "saida/casas_filtradas.csv")
            ),
            filtro_json=data.get("filtro_json"),
            regenerar_filtro_das_imagens=bool(data.get("regenerar_filtro_das_imagens", True)),
            pasta_regioes_interesse=str(
                data.get("pasta_regioes_interesse", "regioes_interesse")
            ),
            queries_busca=list(data.get("queries_busca", [])),
            max_links_por_query=int(data.get("max_links_por_query", 12)),
            google_cse_api_key=data.get("google_cse_api_key") or None,
            google_cse_cx=data.get("google_cse_cx") or None,
            user_agent=str(data.get("user_agent", DEFAULT_UA)),
            pausa_segundos_entre_requisicoes=float(
                data.get("pausa_segundos_entre_requisicoes", 2.0)
            ),
            dominios_permitidos=list(
                data.get("dominios_permitidos", DOMINIOS_PADRAO_PORTAIS)
            ),
            idioma_busca_ddg=str(data.get("idioma_busca_ddg", "br-pt")),
            timeout_http=int(data.get("timeout_http", 35)),
        )


def carregar_config(caminho: str | Path) -> ConfigColeta:
    raw = json.loads(Path(caminho).read_text(encoding="utf-8"))
    return ConfigColeta.from_dict(raw)


def montar_filtro(cfg: ConfigColeta, raiz_projeto: Path) -> FiltroRegioes:
    if cfg.regenerar_filtro_das_imagens or not cfg.filtro_json:
        pasta = raiz_projeto / cfg.pasta_regioes_interesse
        filtro = extrair_regioes_da_pasta(pasta)
    else:
        caminho = raiz_projeto / cfg.filtro_json
        filtro = FiltroRegioes.carregar_json(caminho)
    if cfg.bounding_boxes_por_imagem:
        filtro.aplicar_bbox_por_imagem(cfg.bounding_boxes_por_imagem, bbox_padrao=cfg.bounding_box)
    else:
        filtro.aplicar_bbox(cfg.bounding_box)
    return filtro


def _filtrar_dominio(url: str, permitidos: list[str]) -> bool:
    if not permitidos:
        return True
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in permitidos)


def extrair_links_duckduckgo(
    sessao: requests.Session, query: str, max_links: int, kl: str
) -> list[str]:
    """Busca HTML do DuckDuckGo (POST) e devolve URLs reais a partir dos redirects uddg=."""
    links: list[str] = []
    try:
        r = sessao.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query, "b": "", "kl": kl},
            timeout=35,
        )
        r.raise_for_status()
    except requests.RequestException as e:
        LOG.warning("DuckDuckGo falhou para %r: %s", query, e)
        return links

    soup = BeautifulSoup(r.text, "html.parser")
    candidatos: list[str] = []
    for a in soup.select("a.result__a"):
        href = a.get("href") or ""
        candidatos.append(href)
    if not candidatos:
        for a in soup.select(".result__title a, .result-title a, a.result-link"):
            href = a.get("href") or ""
            candidatos.append(href)

    for href in candidatos:
        if len(links) >= max_links:
            break
        real = _resolver_url_ddg(href)
        if not real:
            continue
        if real not in links:
            links.append(real)
    return links


def _resolver_url_ddg(href: str) -> str | None:
    if not href:
        return None
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if "duckduckgo.com" in (parsed.hostname or ""):
        qs = parse_qs(parsed.query)
        if "uddg" in qs:
            return unquote(qs["uddg"][0])
        if "u" in qs:
            return unquote(qs["u"][0])
        return None
    return href


def extrair_links_google_cse(
    sessao: requests.Session,
    query: str,
    api_key: str,
    cx: str,
    max_links: int,
) -> list[str]:
    links: list[str] = []
    start = 1
    while len(links) < max_links and start <= 91:
        num = min(10, max_links - len(links))
        params = {
            "key": api_key,
            "cx": cx,
            "q": query,
            "num": num,
            "start": start,
        }
        try:
            r = sessao.get(
                "https://www.googleapis.com/customsearch/v1",
                params=params,
                timeout=35,
            )
            r.raise_for_status()
            payload = r.json()
        except (requests.RequestException, json.JSONDecodeError) as e:
            LOG.warning("Google CSE falhou (%s): %s", query, e)
            break
        items = payload.get("items") or []
        if not items:
            break
        for item in items:
            u = item.get("link")
            if u and u not in links:
                links.append(u)
                if len(links) >= max_links:
                    return links
        start += 10
    return links

def coletar_urls(config: ConfigColeta, sessao: requests.Session) -> list[str]:
    vistos: set[str] = set()
    resultado: list[str] = []
    for q in config.queries_busca:
        lds = extrair_links_duckduckgo(
            sessao, q, config.max_links_por_query, config.idioma_busca_ddg
        )
        for u in lds:
            if u not in vistos and _filtrar_dominio(u, config.dominios_permitidos):
                vistos.add(u)
                resultado.append(u)
        time.sleep(config.pausa_segundos_entre_requisicoes)
        if config.google_cse_api_key and config.google_cse_cx:
            gcs = extrair_links_google_cse(
                sessao,
                q,
                config.google_cse_api_key,
                config.google_cse_cx,
                config.max_links_por_query,
            )
            for u in gcs:
                if u not in vistos and _filtrar_dominio(u, config.dominios_permitidos):
                    vistos.add(u)
                    resultado.append(u)
            time.sleep(config.pausa_segundos_entre_requisicoes)
    return resultado


def _texto_visivel(soup: BeautifulSoup) -> str:
    parts: list[str] = []
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    for s in soup.stripped_strings:
        if len(s) > 400:
            parts.append(s[:400])
        else:
            parts.append(s)
    return " \n ".join(parts)


def _telefones_do_texto(texto: str) -> list[str]:
    encontrados = RE_TELEFONE_BR.findall(texto)
    limpos: list[str] = []
    for t in encontrados:
        norm = re.sub(r"\s+", " ", t.strip())
        if norm not in limpos:
            limpos.append(norm)
    return limpos


def _m2_do_texto(texto: str) -> str:
    m = RE_M2.search(texto)
    if not m:
        return ""
    return m.group(1).replace(".", "")


def _eh_titulo_likely_casa(titulo: str, texto: str) -> tuple[bool, str]:
    blob = f"{titulo} {texto}".lower()
    for p in PALAVRAS_EXCLUIR_TIPO:
        if p in blob:
            return False, f"exclusão por tipo: {p}"
    if any(k in blob for k in PALAVRAS_PREFERIR_CASA):
        return True, ""
    # Ambíguo: mantém se não há exclusão explícita (alguns sites só dizem "residencial")
    return True, ""


def _iter_ld_json(html: str) -> Iterator[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script", attrs={"type": lambda x: x and "ld+json" in x}):
        raw = script.string or script.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    yield item
        elif isinstance(data, dict):
            yield data


def extrair_coordenadas_do_html(html: str) -> tuple[float | None, float | None]:
    for block in _iter_ld_json(html):
        lat, lon = _latlng_de_objeto_jsonld(block)
        if lat is not None and lon is not None:
            return lat, lon
    soup = BeautifulSoup(html, "html.parser")
    lat_m = soup.find("meta", attrs={"itemprop": "latitude"}) or soup.find(
        "meta", attrs={"property": "place:location:latitude"}
    )
    lon_m = soup.find("meta", attrs={"itemprop": "longitude"}) or soup.find(
        "meta", attrs={"property": "place:location:longitude"}
    )
    if lat_m and lon_m and lat_m.get("content") and lon_m.get("content"):
        try:
            return float(lat_m["content"]), float(lon_m["content"])
        except ValueError:
            pass
    return None, None


def _latlng_de_objeto_jsonld(obj: dict[str, Any]) -> tuple[float | None, float | None]:
    # Formato comum: "geo": {"@type":"GeoCoordinates","latitude":..,"longitude":..}
    geo = obj.get("geo")
    if isinstance(geo, dict):
        try:
            la = float(geo.get("latitude"))
            lo = float(geo.get("longitude"))
            return la, lo
        except (TypeError, ValueError):
            pass
    # graph
    if "@graph" in obj and isinstance(obj["@graph"], list):
        for node in obj["@graph"]:
            if isinstance(node, dict):
                la, lo = _latlng_de_objeto_jsonld(node)
                if la is not None and lo is not None:
                    return la, lo
    return None, None


def extrair_endereco_do_html(html: str, soup: BeautifulSoup | None = None) -> str:
    if soup is None:
        soup = BeautifulSoup(html, "html.parser")
    for sel in (
        ("meta", {"property": "og:description"}),
        ("meta", {"name": "description"}),
    ):
        tag = soup.find(sel[0], attrs=sel[1])
        if tag and tag.get("content"):
            return tag["content"].strip()[:500]
    for block in _iter_ld_json(html):
        end = block.get("address")
        if end is None:
            obj = block.get("object")
            if isinstance(obj, dict):
                end = obj.get("address")
        if isinstance(end, dict):
            partes = [
                end.get("streetAddress"),
                end.get("addressLocality"),
                end.get("addressRegion"),
            ]
            return ", ".join(p for p in partes if p)
        if isinstance(end, str) and end:
            return end
    texto = _texto_visivel(soup)
    return texto[:400].replace("\n", " ")


def extrair_titulo(soup: BeautifulSoup) -> str:
    t = soup.find("meta", property="og:title") or soup.find("title")
    if t and t.get("content"):
        return t["content"].strip()
    if t and t.string:
        return t.string.strip()
    return ""


def processar_url_anuncio(
    url: str,
    sessao: requests.Session,
    filtro: FiltroRegioes,
    geolocator: Nominatim,
    config: ConfigColeta,
) -> AnuncioCasa | None:
    try:
        r = sessao.get(url, timeout=config.timeout_http, allow_redirects=True)
        r.raise_for_status()
    except requests.RequestException as e:
        LOG.warning("GET %s: %s", url, e)
        return None

    html = r.text
    soup = BeautifulSoup(html, "html.parser")
    titulo = extrair_titulo(soup)
    texto = _texto_visivel(soup)
    ok_tipo, motivo = _eh_titulo_likely_casa(titulo, texto)
    anuncio = AnuncioCasa(
        endereco="",
        tamanho_m2="",
        link=r.url,
        telefone_imobiliaria="",
        telefone_vendedor="",
        fonte=urlparse(r.url).hostname or "",
        titulo=titulo,
        motivo_exclusao="",
    )
    if not ok_tipo:
        anuncio.motivo_exclusao = motivo
        return None

    anuncio.endereco = extrair_endereco_do_html(html, soup=soup)
    anuncio.tamanho_m2 = _m2_do_texto(texto) or _m2_do_texto(titulo)

    phones = _telefones_do_texto(texto)
    if phones:
        anuncio.telefone_imobiliaria = phones[0]
        anuncio.telefone_vendedor = phones[1] if len(phones) > 1 else ""

    lat, lon = extrair_coordenadas_do_html(html)
    if lat is None or lon is None:
        try:
            q = f"{anuncio.endereco}, São Paulo, Brasil"
            loc = geolocator.geocode(q, timeout=15, exactly_one=True)
            time.sleep(1.05)  # política de uso Nominatim
            if loc:
                lat, lon = loc.latitude, loc.longitude
        except (GeocoderTimedOut, GeocoderServiceError, Exception) as e:
            LOG.debug("Geocodificação falhou: %s", e)
            lat, lon = None, None

    if lat is None or lon is None:
        return None

    anuncio.latitude = str(lat)
    anuncio.longitude = str(lon)

    if not filtro.contem_coordenadas(lat, lon):
        return None

    for reg in filtro.regioes:
        verts = reg.poligono.vertices_latlng
        if len(verts) < 3:
            continue
        anel = [(lng, la) for la, lng in verts]
        if ponto_em_poligono(lon, lat, anel):
            anuncio.regiao_poligono_id = reg.id_regiao
            break

    return anuncio


def garantir_pasta_saida(caminho_csv: Path) -> None:
    caminho_csv.parent.mkdir(parents=True, exist_ok=True)


def salvar_csv(anuncios: list[AnuncioCasa], caminho: Path) -> None:
    garantir_pasta_saida(caminho)
    campos = [
        "endereco",
        "tamanho_m2",
        "link",
        "telefone_imobiliaria",
        "telefone_vendedor",
        "fonte",
        "regiao_poligono_id",
        "latitude",
        "longitude",
        "titulo",
        "motivo_exclusao",
    ]
    with caminho.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=campos, extrasaction="ignore")
        w.writeheader()
        for a in anuncios:
            w.writerow(a.linha_csv())


def rodar_coleta(config: ConfigColeta, raiz_projeto: Path | None = None) -> Path:
    raiz = raiz_projeto or Path(__file__).resolve().parent
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    filtro = montar_filtro(config, raiz)
    LOG.info("Filtro carregado: %s polígonos.", len(filtro.regioes))

    sessao = requests.Session()
    sessao.headers.update({"User-Agent": config.user_agent, "Accept-Language": "pt-BR,pt;q=0.9"})

    urls = coletar_urls(config, sessao)
    LOG.info("%s URLs candidatas após busca.", len(urls))

    geolocator = Nominatim(user_agent=config.user_agent[:120], timeout=20)
    anuncios: list[AnuncioCasa] = []
    for u in urls:
        a = processar_url_anuncio(u, sessao, filtro, geolocator, config)
        if a:
            anuncios.append(a)
            LOG.info("Aceito: %s", a.link)
        time.sleep(config.pausa_segundos_entre_requisicoes)

    out = raiz / config.arquivo_saida_csv
    salvar_csv(anuncios, out)
    LOG.info("CSV gravado em %s (%s linhas).", out, len(anuncios))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Coleta casas à venda filtradas geograficamente.")
    ap.add_argument("--config", required=True, help="JSON de configuração (ver config_coleta.exemplo.json)")
    ap.add_argument("--raiz", default=None, help="Pasta raiz do projeto (padrão: diretório deste arquivo)")
    args = ap.parse_args()
    raiz = Path(args.raiz).resolve() if args.raiz else None
    cfg = carregar_config(args.config)
    rodar_coleta(cfg, raiz)


if __name__ == "__main__":
    main()
