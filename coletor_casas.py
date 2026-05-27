"""
coletor_casas.py
----------------
Coleta links de anúncios na web, extrai dados de páginas de imóveis e mantém apenas
anúncios que passam pelo filtro geográfico (`filtro_regioes.FiltroRegioes`).

Dependências:
    pip install requests beautifulsoup4 geopy ddgs typing-extensions curl-cffi

Observações:
    - Sites grandes usam Cloudflare/WAF: o coletor tenta usar **curl-cffi** (fingerprint de navegador)
      para reduzir HTTP 403. Se ainda bloquear, só navegador real (ex. Playwright) tende a passar.
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
from typing import Any, Iterable, Iterator
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests

try:
    from curl_cffi.requests.exceptions import HTTPError as CffiHTTPError
    from curl_cffi.requests.exceptions import RequestException as CffiRequestException

    _ERROS_HTTP_SESSAO: tuple[type[BaseException], ...] = (
        requests.RequestException,
        CffiRequestException,
        CffiHTTPError,
    )
except ImportError:
    _ERROS_HTTP_SESSAO = (requests.RequestException,)

_ERROS_HTTP_OU_JSON: tuple[type[BaseException], ...] = (
    *_ERROS_HTTP_SESSAO,
    json.JSONDecodeError,
)

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

RE_ID_ANUNCIO_URL = re.compile(r"-id-(\d+)\b", re.IGNORECASE)
RE_TEL_HREF = re.compile(r"tel:([+\d\s\-().]+)", re.IGNORECASE)
RE_TELEFONE_JSON = re.compile(
    r'"(?:phone|telephone|mobilePhone|phones|whatsapp|contactPhone)"\s*:\s*"([^"]+)"',
    re.IGNORECASE,
)
# Celular (11) 9xxxx-xxxx ou fixo (11) xxxx-xxxx (com separadores)
RE_TELEFONE_BR = re.compile(
    r"(?:\+?55\s*)?"
    r"(?:\(\s*(\d{2})\s*\)|(?<!\d)(\d{2})(?!\d))\s*"
    r"((?:9\d{4}[-.\s]?\d{4})|(?:[2-5]\d{3}[-.\s]?\d{4}))\b"
)
# Celular/fixos colados no HTML/JS (ex.: 11971236912) — só com validação posterior
RE_CELULAR_EMBUTIDO = re.compile(r"(?<!\d)([1-9]\d9\d{8})(?!\d)")
RE_FIXO_EMBUTIDO = re.compile(r"(?<!\d)([1-9]\d[2-5]\d{7})(?!\d)")

RE_M2 = re.compile(
    r"(\d{1,3}(?:\.\d{3})*|\d+)\s*(?:m²|m2|metros?\s*quadrados?)",
    re.IGNORECASE,
)
# Metragem em slug de URL (ex.: ...-100m2-venda-...)
RE_M2_SLUG = re.compile(r"(\d{1,4})m2(?:-|$)", re.IGNORECASE)

# Endereço em linha (Viva Real / Zap no HTML) — ex.: Rua X, 330 - Itaquera, São Paulo - SP
RE_ENDERECO_LINHA = re.compile(
    r"((?:Rua|R\.|Av\.|Avenida|Travessa|Alameda|Praça|Estrada|Rod\.)\s"
    r"[^<\"\\]{5,90}?"
    r",\s*\d+"
    r"[^<\"\\]{0,55}?"
    r"-\s*SP\b)",
    re.IGNORECASE,
)
RE_CEP_BR = re.compile(r"\b(0[1-9]\d{3})-?(\d{3})\b")
RE_CEP_ROTULO = re.compile(
    r"(?:CEP|cep|postalCode|zipCode)[\"'\s:]*[\"'\s]*(\d{5})-?(\d{3})",
    re.IGNORECASE,
)

# Parâmetros uddg= em qualquer HTML retornado pelo DuckDuckGo
RE_UDDG_PARAM = re.compile(r"uddg=([^&\"'<>]+)", re.IGNORECASE)

# Links de anúncio embutidos em HTML/JSON (portais atuais)
RE_URL_VIVAREAL = re.compile(
    r"https?://(?:www\.)?vivareal\.com\.br/imovel/[a-z0-9\-]+-id-\d+/?",
    re.IGNORECASE,
)
RE_PATH_VIVAREAL = re.compile(
    r"/imovel/[a-z0-9\-]+-id-\d+/?",
    re.IGNORECASE,
)
RE_URL_ZAP = re.compile(
    r"https?://(?:www\.)?zapimoveis\.com\.br/imovel/[a-z0-9\-]+-id-[\w\d]+/?",
    re.IGNORECASE,
)
RE_PATH_ZAP = re.compile(
    r"/imovel/[a-z0-9\-]+-id-[\w\d]+/?",
    re.IGNORECASE,
)
RE_URL_OLX = re.compile(
    r"https?://[a-z0-9.-]*olx\.com\.br/(?:d/)?(?:ad|vi)/[a-z0-9\-]+/?",
    re.IGNORECASE,
)
RE_URL_IMOVELWEB = re.compile(
    r"https?://(?:www\.)?imovelweb\.com\.br/imovel/[^\"'\s<>?#]+",
    re.IGNORECASE,
)
RE_NEXT_DATA = re.compile(
    r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)

# Páginas de listagem conhecidas (regiões do projeto) — expandidas para links de anúncio
URLS_HUB_PADRAO: list[str] = [
    "https://www.vivareal.com.br/venda/sp/sao-paulo/zona-leste/cidade-patriarca/casa_residencial/",
    "https://www.vivareal.com.br/venda/sp/sao-paulo/zona-leste/itaquera/casa_residencial/",
]

# Bairros alinhados aos hubs do projeto (fallback textual se polígono da imagem não calibrar)
BAIRROS_INTERESSE_PADRAO: tuple[str, ...] = (
    "patriarca",
    "cidade patriarca",
    "vila patriarca",
    "itaquera",
    "dom bosco",
    "cidade líder",
    "cidade lider",
    "jardim helena",
    "vila carmosina",
    "são mateus",
    "sao mateus",
    "artur alvim",
    "zona leste",
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
    urls_paginas_hub: list[str] = field(default_factory=list)
    expandir_hub_max_links: int = 80
    usar_ddgs_api: bool = True
    usar_curl_cffi: bool = True
    curl_impersonate: str = "chrome"
    aquecer_sessao: bool = True
    somente_venda: bool = True
    priorizar_vivareal: bool = True
    usar_busca_web: bool = False
    filtro_geo: str = "poligono_ou_bbox"
    bairros_interesse: list[str] = field(
        default_factory=lambda: list(BAIRROS_INTERESSE_PADRAO)
    )
    max_anuncios_processar: int = 50

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
        if "urls_paginas_hub" in data and isinstance(data["urls_paginas_hub"], list):
            urls_hub = [str(u) for u in data["urls_paginas_hub"]]
        else:
            urls_hub = list(URLS_HUB_PADRAO)
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
            urls_paginas_hub=urls_hub,
            expandir_hub_max_links=int(data.get("expandir_hub_max_links", 80)),
            usar_ddgs_api=bool(data.get("usar_ddgs_api", True)),
            usar_curl_cffi=bool(data.get("usar_curl_cffi", True)),
            curl_impersonate=str(data.get("curl_impersonate", "chrome")),
            aquecer_sessao=bool(data.get("aquecer_sessao", True)),
            somente_venda=bool(data.get("somente_venda", True)),
            priorizar_vivareal=bool(data.get("priorizar_vivareal", True)),
            usar_busca_web=bool(data.get("usar_busca_web", False)),
            filtro_geo=str(data.get("filtro_geo", "poligono_ou_bbox")),
            bairros_interesse=list(
                data.get("bairros_interesse", BAIRROS_INTERESSE_PADRAO)
            ),
            max_anuncios_processar=int(data.get("max_anuncios_processar", 50)),
        )


def carregar_config(caminho: str | Path) -> ConfigColeta:
    raw = json.loads(Path(caminho).read_text(encoding="utf-8"))
    return ConfigColeta.from_dict(raw)


def criar_sessao_http(config: ConfigColeta):
    headers = {"User-Agent": config.user_agent, "Accept-Language": "pt-BR,pt;q=0.9"}
    if config.usar_curl_cffi:
        try:
            from curl_cffi import requests as cf_requests

            sess = cf_requests.Session(impersonate=config.curl_impersonate)
            sess.headers.update(headers)
            LOG.info(
                "Sessão HTTP: curl-cffi (impersonate=%s).",
                config.curl_impersonate,
            )
            return sess
        except Exception as exc:
            LOG.warning(
                "curl-cffi indisponível (%s); usando requests puro (mais suscetível a 403).",
                exc,
            )
    sessao = requests.Session()
    sessao.headers.update(headers)
    LOG.info("Sessão HTTP: requests.")
    return sessao


def aquecer_sessao_portais(sessao: Any, config: ConfigColeta) -> None:
    """Visita a home dos portais para obter cookies antes de listagens/anúncios."""
    if not config.aquecer_sessao:
        return
    portais = ["https://www.vivareal.com.br/"]
    if not config.priorizar_vivareal:
        portais.append("https://www.zapimoveis.com.br/")
    for url in portais:
        try:
            sessao.get(url, timeout=min(config.timeout_http, 25), allow_redirects=True)
        except _ERROS_HTTP_SESSAO as e:
            LOG.debug("Aquecimento %s: %s", url, e)


def montar_filtro(cfg: ConfigColeta, raiz_projeto: Path) -> FiltroRegioes:
    if cfg.regenerar_filtro_das_imagens or not cfg.filtro_json:
        pasta = raiz_projeto / cfg.pasta_regioes_interesse
        filtro = extrair_regioes_da_pasta(pasta).consolidar_por_imagem_casco_convexo()
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


def normalizar_url_anuncio(url: str) -> str:
    """Remove parâmetros de rastreio e fragmentos; mantém path canônico."""
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        return url
    try:
        p = urlparse(url)
    except ValueError:
        return url
    path = p.path or ""
    if path and not path.endswith("/"):
        host = (p.hostname or "").lower()
        if any(d in host for d in ("vivareal.com.br", "zapimoveis.com.br", "imovelweb.com.br")):
            path = path + "/"
    limpo = p._replace(path=path, query="", fragment="")
    return limpo.geturl()


def parece_url_anuncio_individual(url: str) -> bool:
    """Identifica páginas de detalhe (não listagens só de bairro)."""
    url = normalizar_url_anuncio(url)
    try:
        p = urlparse(url)
    except ValueError:
        return False
    host = (p.hostname or "").lower()
    path = (p.path or "").lower()
    if "vivareal.com.br" in host:
        # Antigo: /imovel/venda/... | Atual: /imovel/casa-...-venda-RS...-id-123456/
        if "/imovel/venda/" in path:
            return True
        return path.startswith("/imovel/") and "-id-" in path
    if "zapimoveis.com.br" in host:
        if path.startswith("/imovel/") and "-id-" in path:
            return True
        return "/imovel/" in path and (
            "venda" in path or "pra-venda" in path or "para-venda" in path
        )
    if "imovelweb.com.br" in host:
        return "/imovel/" in path and len(path) > 24 and "propriedades" not in path
    if "chavesnamao.com.br" in host:
        return "/imovel" in path
    if "olx.com.br" in host:
        return "/d/ad/" in path or "/vi/" in path or ("/d/" in path and path.count("/") >= 5)
    if "mercadolivre.com.br" in host or "mercadolivre.com" in host:
        upper = url.upper()
        return "/p/" in path or "MLB-" in upper
    return False


def eh_url_venda(url: str) -> bool:
    """Descarta URLs de aluguel quando somente_venda está ativo."""
    path = (urlparse(url).path or "").lower()
    if any(
        x in path
        for x in (
            "aluguel",
            "para-alugar",
            "para-alugar",
            "/alugar/",
            "locacao",
            "locação",
        )
    ):
        return False
    return "venda" in path or "-venda-" in path or "/imovel/venda" in path


def url_espelho_vivareal(url: str) -> str | None:
    """Mesmo anúncio costuma existir no Viva Real (backend Grupo ZAP/OLX)."""
    try:
        p = urlparse(url)
    except ValueError:
        return None
    host = (p.hostname or "").lower()
    if "zapimoveis.com.br" not in host:
        return None
    return url.replace(host, "www.vivareal.com.br")


def _referer_para_url(url: str) -> str | None:
    host = (urlparse(url).hostname or "").lower()
    if "vivareal.com.br" in host:
        return "https://www.vivareal.com.br/"
    if "zapimoveis.com.br" in host:
        return "https://www.zapimoveis.com.br/"
    if "imovelweb.com.br" in host:
        return "https://www.imovelweb.com.br/"
    if "olx.com.br" in host:
        return "https://www.olx.com.br/"
    return None


def _walk_obj_por_urls(obj: Any, acc: list[str]) -> None:
    if isinstance(obj, str) and obj.startswith("http") and parece_url_anuncio_individual(obj):
        acc.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            _walk_obj_por_urls(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            _walk_obj_por_urls(v, acc)


def _extrair_urls_de_next_data(html: str) -> list[str]:
    m = RE_NEXT_DATA.search(html)
    if not m:
        return []
    try:
        payload = json.loads(m.group(1))
    except json.JSONDecodeError:
        return []
    acc: list[str] = []
    _walk_obj_por_urls(payload, acc)
    return acc


def _extrair_urls_regex_portais(html: str, base_url: str) -> list[str]:
    found: list[str] = []
    base_host = (urlparse(base_url).hostname or "").lower()

    def _push(u: str) -> None:
        if u not in found:
            found.append(u)

    for rx in (RE_URL_VIVAREAL, RE_URL_ZAP, RE_URL_OLX, RE_URL_IMOVELWEB):
        for m in rx.finditer(html):
            _push(m.group(0).rstrip("\\\",')"))

    if "vivareal.com.br" in base_host:
        for m in RE_PATH_VIVAREAL.finditer(html):
            _push(urljoin("https://www.vivareal.com.br", m.group(0)))
    if "zapimoveis.com.br" in base_host:
        for m in RE_PATH_ZAP.finditer(html):
            _push(urljoin("https://www.zapimoveis.com.br", m.group(0)))
    # JSON embutido costuma trazer só o path /imovel/...-id-...
    if "vivareal.com.br" in html or RE_PATH_VIVAREAL.search(html):
        for m in RE_PATH_VIVAREAL.finditer(html):
            _push(urljoin("https://www.vivareal.com.br", m.group(0)))

    return found


def extrair_urls_anuncio_do_html(
    html: str, base_url: str, permitidos: list[str]
) -> list[str]:
    """Extrai links de anúncio via âncoras, regex e JSON embutido (__NEXT_DATA__)."""
    vistos: set[str] = set()
    out: list[str] = []

    def _add(raw: str) -> None:
        absolute = urljoin(base_url, raw.strip())
        absolute = normalizar_url_anuncio(absolute.split("#", 1)[0])
        if not _filtrar_dominio(absolute, permitidos):
            return
        if not parece_url_anuncio_individual(absolute):
            return
        if absolute not in vistos:
            vistos.add(absolute)
            out.append(absolute)

    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href and not href.startswith(("#", "javascript:")):
            _add(href)
    for u in _extrair_urls_regex_portais(html, base_url):
        _add(u)
    for u in _extrair_urls_de_next_data(html):
        _add(u)
    return out


def expandir_links_de_paginas_hub(
    sessao: Any,
    url_hub: str,
    permitidos: list[str],
    max_links: int,
    timeout: int,
) -> list[str]:
    """Baixa página de listagem de um portal e extrai URLs de anúncios individuais."""
    out: list[str] = []
    try:
        ref = _referer_para_url(url_hub) or url_hub
        home = _referer_para_url(url_hub) or "https://www.vivareal.com.br/"
        try:
            sessao.get(home, timeout=timeout, allow_redirects=True)
        except _ERROS_HTTP_SESSAO:
            pass
        r = sessao.get(
            url_hub.strip(),
            timeout=timeout,
            allow_redirects=True,
            headers={"Referer": ref},
        )
        if r.status_code != 200:
            LOG.warning("Hub %s: HTTP %s", url_hub[:80], r.status_code)
            return out
        if len(r.text) < 50_000:
            LOG.warning(
                "Hub %s: resposta curta (%s bytes) — possível bloqueio; "
                "confira curl-cffi instalado.",
                url_hub[:60],
                len(r.text),
            )
        candidatos = extrair_urls_anuncio_do_html(r.text, r.url, permitidos)
        out = candidatos[:max_links]
        if not out:
            LOG.info(
                "Hub %s: página OK mas nenhum link de anúncio no HTML (pode ser SPA bloqueada).",
                url_hub[:72],
            )
    except _ERROS_HTTP_SESSAO as e:
        LOG.warning("Erro ao expandir hub %s: %s", url_hub[:80], e)
    return out


def _extrair_urls_do_html_via_regex_uddg(html: str) -> list[str]:
    resultado: list[str] = []
    for m in RE_UDDG_PARAM.finditer(html):
        token = m.group(1).replace("&amp;", "&")
        try:
            cand = unquote(token)
        except Exception:
            cand = token
        if cand.startswith("//"):
            cand = "https:" + cand
        if cand.startswith(("http://", "https://")) and cand not in resultado:
            resultado.append(cand)
    return resultado


def extrair_links_ddgs_python(query: str, max_links: int, region: str) -> list[str]:
    """Camada oficial moderna DuckDuckGo (pacote pip `ddgs`, sucessor do duckduckgo-search)."""
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS  # retrocompatível
    except ImportError:
        LOG.warning(
            'Instale o cliente DDG: `pip install ddgs typing-extensions`.'
        )
        return []
    cap = min(50, max(30, max_links * 5))
    out: list[str] = []
    try:
        with DDGS() as ddgs:
            kwargs: dict[str, Any] = {"max_results": cap}
            if region:
                kwargs["region"] = region
            for item in ddgs.text(query, **kwargs):
                u = item.get("href") or item.get("url")
                if isinstance(u, str) and u.startswith("http"):
                    if u not in out:
                        out.append(u)
                if len(out) >= cap:
                    break
    except Exception as e:
        LOG.warning("duckduckgo_search (%r): %s", query, e)
    return out


def extrair_links_duckduckgo(
    sessao: Any,
    query: str,
    max_links: int,
    kl: str,
    timeout: int,
) -> list[str]:
    """Varre HTML DuckDuckGo (POST html + lite) com seletores e regex sobre uddg=."""
    limite_extra = max(max_links * 3, 30)
    found: list[str] = []

    def _append(u: str | None) -> None:
        if (
            u
            and u.startswith("http")
            and u not in found
            and len(found) < limite_extra
        ):
            found.append(u)

    try:
        r = sessao.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query, "b": "", "kl": kl},
            timeout=timeout,
        )
        r.raise_for_status()
        corpo = r.text
    except _ERROS_HTTP_SESSAO as e:
        LOG.warning("DuckDuckGo HTML falhou para %r: %s", query, e)
        corpo = ""

    if corpo:
        for u in _extrair_urls_do_html_via_regex_uddg(corpo):
            _append(u)
        soup = BeautifulSoup(corpo, "html.parser")
        candidatos = [
            *[a.get("href") or "" for a in soup.select("a.result__a")],
            *[
                a.get("href") or ""
                for a in soup.select(
                    '.result__title a, .result-title a, a.result-link, a[data-testid="result-title-a"]'
                )
            ],
        ]
        for href in candidatos:
            resolved = _resolver_url_ddg(href)
            _append(resolved)

    # Versão lite (fallback)
    try:
        rl = sessao.get(
            "https://lite.duckduckgo.com/lite/",
            params={"q": query, "kl": kl},
            timeout=timeout,
        )
        if rl.status_code == 200:
            for u in _extrair_urls_do_html_via_regex_uddg(rl.text):
                _append(u)
            sp = BeautifulSoup(rl.text, "html.parser")
            for row in sp.select("table tr td a"):
                hu = row.get("href") or ""
                resolved = _resolver_url_ddg(hu)
                _append(resolved)
    except _ERROS_HTTP_SESSAO as e:
        LOG.debug("DDG lite falhou (%r): %s", query, e)

    return found[:limite_extra]


def extrair_links_bing(
    sessao: Any, query: str, max_links: int, timeout: int
) -> list[str]:
    links: list[str] = []
    try:
        r = sessao.get(
            "https://www.bing.com/search",
            params={"q": query},
            timeout=timeout,
        )
        r.raise_for_status()
    except _ERROS_HTTP_SESSAO as e:
        LOG.warning("Bing falhou (%r): %s", query, e)
        return links
    raw = r.text
    lowered = raw.lower()
    if "turnstile" in lowered or "class=\"captcha\"" in lowered.replace("'", '"'):
        LOG.info(
            "Bing retornou desafio (captcha) para %r; ignorando resultados Bing nesta rodada.",
            query[:50],
        )
        return links
    soup = BeautifulSoup(raw, "html.parser")
    for sel in ("li.b_algo h2 a", "h2.b_title a"):
        for a in soup.select(sel):
            href = (a.get("href") or "").strip()
            if not href.startswith("http"):
                href = urljoin(r.url, href)
            parsed = urlparse(href)
            if parsed.scheme not in ("http", "https"):
                continue
            if "bing.com" in (parsed.hostname or ""):
                continue
            if href not in links:
                links.append(href)
            if len(links) >= max_links * 3:
                return links[: max_links * 3]
    return links[: max_links * 3]


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
    sessao: Any,
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
        except _ERROS_HTTP_OU_JSON as e:
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


def coletar_urls(config: ConfigColeta, sessao: Any) -> list[str]:
    vistos: set[str] = set()
    resultado: list[str] = []

    def add_batch(origem: Iterable[str], origem_busca: bool = False) -> tuple[int, int]:
        antes = len(resultado)
        for u in origem:
            u_norm = normalizar_url_anuncio(u)
            if u_norm in vistos:
                continue
            if not _filtrar_dominio(u_norm, config.dominios_permitidos):
                continue
            host = (urlparse(u_norm).hostname or "").lower()
            if config.priorizar_vivareal and origem_busca and "zapimoveis.com.br" in host:
                continue
            if config.somente_venda and not eh_url_venda(u_norm):
                continue
            if not parece_url_anuncio_individual(u_norm):
                continue
            vistos.add(u_norm)
            resultado.append(u_norm)
        return len(resultado) - antes, len(resultado)

    for hub in config.urls_paginas_hub:
        uhub = hub.strip()
        if not uhub:
            continue
        sub = expandir_links_de_paginas_hub(
            sessao,
            uhub,
            config.dominios_permitidos,
            config.expandir_hub_max_links,
            config.timeout_http,
        )
        novas, tot = add_batch(sub)
        LOG.info(
            "Expansão de listagem (%s…) → %s novas URLs (total candidatas=%s).",
            uhub[:72],
            novas,
            tot,
        )
        time.sleep(config.pausa_segundos_entre_requisicoes)

    if config.usar_busca_web:
        for q in config.queries_busca:
            bloco: list[str] = []
            if config.usar_ddgs_api:
                bloco.extend(
                    extrair_links_ddgs_python(
                        q, config.max_links_por_query, config.idioma_busca_ddg
                    )
                )
            bloco.extend(
                extrair_links_duckduckgo(
                    sessao,
                    q,
                    config.max_links_por_query,
                    config.idioma_busca_ddg,
                    config.timeout_http,
                )
            )
            bloco.extend(
                extrair_links_bing(
                    sessao, q, config.max_links_por_query, config.timeout_http
                )
            )
            novas, tot = add_batch(bloco, origem_busca=True)
            LOG.info(
                "Busca %r → %s nova(s) URL; total candidatas=%s.",
                q[:76],
                novas,
                tot,
            )
            time.sleep(config.pausa_segundos_entre_requisicoes)
            if config.google_cse_api_key and config.google_cse_cx:
                gcs = extrair_links_google_cse(
                    sessao,
                    q,
                    config.google_cse_api_key,
                    config.google_cse_cx,
                    config.max_links_por_query,
                )
                novas_g, tot = add_batch(gcs, origem_busca=True)
                LOG.info(
                    "Google CSE (%s…) → %s novas URLs; total=%s.",
                    q[:52],
                    novas_g,
                    tot,
                )
                time.sleep(config.pausa_segundos_entre_requisicoes)

    def _prio(u: str) -> tuple[int, str]:
        host = (urlparse(u).hostname or "").lower()
        if "vivareal.com.br" in host:
            return (0, u)
        return (1, u)

    resultado.sort(key=_prio)
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


def _so_digitos(valor: str) -> str:
    return re.sub(r"\D", "", valor or "")


def _ids_excluir_da_url(url: str) -> set[str]:
    return {m.group(1) for m in RE_ID_ANUNCIO_URL.finditer(url or "")}


def _formatar_telefone_br(digits: str) -> str:
    if len(digits) == 13 and digits.startswith("55"):
        digits = digits[2:]
    if len(digits) == 11:
        return f"({digits[:2]}) {digits[2:7]}-{digits[7:]}"
    if len(digits) == 10:
        return f"({digits[:2]}) {digits[2:6]}-{digits[6:]}"
    return digits


def _telefone_br_valido(digits: str, ids_excluir: set[str]) -> bool:
    if not digits or digits in ids_excluir:
        return False
    if len(digits) == 13 and digits.startswith("55"):
        digits = digits[2:]
    if len(digits) not in (10, 11):
        return False
    try:
        ddd = int(digits[:2])
    except ValueError:
        return False
    if ddd < 11 or ddd > 99:
        return False
    if len(digits) == 11:
        return digits[2] == "9"
    return digits[2] in "2345"


def _registrar_telefone(
    bruto: str,
    ids_excluir: set[str],
    vistos: set[str],
    saida: list[str],
) -> None:
    digits = _so_digitos(bruto)
    if not _telefone_br_valido(digits, ids_excluir):
        return
    fmt = _formatar_telefone_br(digits)
    if fmt not in vistos:
        vistos.add(fmt)
        saida.append(fmt)


def extrair_telefones_do_html(
    html: str,
    url: str,
    soup: BeautifulSoup | None = None,
) -> list[str]:
    """Extrai telefones BR reais; ignora IDs de anúncio e números inválidos."""
    ids_excluir = _ids_excluir_da_url(url)
    vistos: set[str] = set()
    saida: list[str] = []

    if soup is None:
        soup = BeautifulSoup(html, "html.parser")

    for a in soup.find_all("a", href=True):
        href = a.get("href") or ""
        if href.lower().startswith("tel:"):
            _registrar_telefone(href[4:], ids_excluir, vistos, saida)
        if "whatsapp" in href.lower() or "wa.me" in href.lower():
            for m in re.finditer(r"\d{10,13}", href):
                _registrar_telefone(m.group(0), ids_excluir, vistos, saida)

    for m in RE_TELEFONE_JSON.finditer(html):
        _registrar_telefone(m.group(1), ids_excluir, vistos, saida)

    for m in RE_TEL_HREF.finditer(html):
        _registrar_telefone(m.group(1), ids_excluir, vistos, saida)

    for ddd, corpo in RE_TELEFONE_BR.findall(html):
        _registrar_telefone(f"{ddd}{_so_digitos(corpo)}", ids_excluir, vistos, saida)

    bloco = _texto_visivel(soup)
    for ddd, corpo in RE_TELEFONE_BR.findall(bloco):
        _registrar_telefone(f"{ddd}{_so_digitos(corpo)}", ids_excluir, vistos, saida)

    for m in RE_CELULAR_EMBUTIDO.finditer(bloco):
        _registrar_telefone(m.group(1), ids_excluir, vistos, saida)
    for m in RE_FIXO_EMBUTIDO.finditer(bloco):
        _registrar_telefone(m.group(1), ids_excluir, vistos, saida)
    if not saida:
        for m in RE_CELULAR_EMBUTIDO.finditer(html):
            _registrar_telefone(m.group(1), ids_excluir, vistos, saida)
        for m in RE_FIXO_EMBUTIDO.finditer(html):
            _registrar_telefone(m.group(1), ids_excluir, vistos, saida)

    return _priorizar_telefones(saida)


def _priorizar_telefones(phones: list[str]) -> list[str]:
    """Celular primeiro; remove centrais genéricas; prioriza DDD 11 (SP)."""
    celulares: list[str] = []
    outros: list[str] = []
    for p in phones:
        dig = _so_digitos(p)
        if len(dig) == 11 and dig[2] == "9":
            celulares.append(p)
        elif len(dig) == 10:
            if dig.endswith("0000") or dig[2:4] in ("00", "30", "40", "80"):
                continue
            outros.append(p)
        else:
            outros.append(p)
    if celulares:
        sp = [p for p in celulares if _so_digitos(p).startswith("11")]
        if sp:
            celulares = sp
    if outros:
        sp_fixo = [p for p in outros if _so_digitos(p).startswith("11")]
        if sp_fixo:
            outros = sp_fixo
    return celulares + outros


def _m2_do_texto(texto: str) -> str:
    m = RE_M2.search(texto)
    if not m:
        m = RE_M2_SLUG.search(texto)
    if not m:
        return ""
    return m.group(1).replace(".", "")


def extrair_m2_do_html(html: str, texto: str, titulo: str, url: str = "") -> str:
    m = _m2_do_texto(texto) or _m2_do_texto(titulo) or _m2_do_texto(url)
    if m:
        return m
    for block in _iter_ld_json(html):
        fs = block.get("floorSize")
        if isinstance(fs, dict):
            val = fs.get("value") or fs.get("@value")
            if val is not None:
                try:
                    return str(int(float(val)))
                except (TypeError, ValueError):
                    pass
        elif isinstance(fs, (int, float)):
            return str(int(fs))
        elif isinstance(fs, str) and fs.strip():
            digits = re.sub(r"[^\d]", "", fs)
            if digits:
                return digits
    return ""


def _tipo_imovel_no_slug(url: str) -> str | None:
    path = (urlparse(url).path or "").lower()
    for tipo in (
        "casa",
        "sobrado",
        "apartamento",
        "cobertura",
        "terreno",
        "kitnet",
        "galpao",
        "galpão",
    ):
        if f"/{tipo}-" in path or f"/imovel/{tipo}" in path:
            return tipo.replace("ã", "a")
    return None


def _eh_titulo_likely_casa(titulo: str, texto: str, url: str = "") -> tuple[bool, str]:
    titulo_l = (titulo or "").lower()
    tipo_slug = _tipo_imovel_no_slug(url) if url else None

    if tipo_slug in ("casa", "sobrado"):
        return True, ""
    if tipo_slug in ("apartamento", "cobertura", "terreno", "kitnet", "galpao"):
        return False, f"tipo no link: {tipo_slug}"

    # Evita rodapé/menu ("apartamentos na região") — usa título + início da descrição
    blob = f"{titulo_l} {(texto or '')[:2500]}".lower()
    for p in PALAVRAS_EXCLUIR_TIPO:
        if p in blob:
            return False, f"exclusão por tipo: {p}"
    if any(k in blob for k in PALAVRAS_PREFERIR_CASA):
        return True, ""
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


def _coordenadas_embutidas_no_html(html: str) -> tuple[float | None, float | None]:
    """Viva Real / Zap costumam embutir lat/lon no HTML/JS (fora do JSON-LD)."""
    # Par lat/lon explícito em JSON
    m = re.search(
        r'["\']?(?:lat|latitude)["\']?\s*[:=]\s*(-?\d{1,2}\.\d{4,})'
        r'[^0-9\-]{0,40}["\']?(?:lng|lon|longitude)["\']?\s*[:=]\s*(-?\d{1,3}\.\d{4,})',
        html,
        re.IGNORECASE,
    )
    if m:
        try:
            return float(m.group(1)), float(m.group(2))
        except ValueError:
            pass
    # Faixa típica Grande São Paulo (projeto focado Zona Leste)
    lats = re.findall(r"-23\.\d{5,}", html)
    lons = re.findall(r"-46\.\d{5,}", html)
    if lats and lons:
        try:
            return float(lats[0]), float(lons[0])
        except ValueError:
            pass
    return None, None


def extrair_coordenadas_do_html(html: str) -> tuple[float | None, float | None]:
    for block in _iter_ld_json(html):
        lat, lon = _latlng_de_objeto_jsonld(block)
        if lat is not None and lon is not None:
            return lat, lon
    lat, lon = _coordenadas_embutidas_no_html(html)
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


def _limpar_texto_endereco(texto: str) -> str:
    t = re.sub(r"\s+", " ", (texto or "").strip())
    t = t.replace("\\u002F", "/")
    return t[:500]


def _endereco_de_dict(end: dict[str, Any]) -> str:
    partes: list[str] = []
    rua = end.get("streetAddress") or end.get("street")
    num = end.get("streetNumber")
    if rua:
        partes.append(f"{rua}, {num}" if num else str(rua))
    bairro = end.get("addressNeighborhood") or end.get("neighborhood")
    cidade = end.get("addressLocality") or end.get("city")
    uf = end.get("addressRegion") or end.get("state")
    if bairro:
        partes.append(str(bairro))
    if cidade or uf:
        loc = ", ".join(p for p in (cidade, uf) if p)
        if loc:
            partes.append(loc)
    cep = end.get("postalCode") or end.get("zipCode")
    if cep:
        dig = re.sub(r"\D", "", str(cep))
        if len(dig) == 8:
            partes.append(f"CEP {dig[:5]}-{dig[5:]}")
    return _limpar_texto_endereco(", ".join(partes))


def _walk_endereco_json(obj: Any, acc: list[str]) -> None:
    if isinstance(obj, dict):
        keys = set(obj.keys())
        if keys & {
            "streetAddress",
            "street",
            "postalCode",
            "zipCode",
            "addressLocality",
            "neighborhood",
        }:
            txt = _endereco_de_dict(obj)
            if txt and txt not in acc:
                acc.append(txt)
        for v in obj.values():
            _walk_endereco_json(v, acc)
    elif isinstance(obj, list):
        for item in obj[:120]:
            _walk_endereco_json(item, acc)


def _extrair_cep_do_html(html: str) -> str | None:
    m = RE_CEP_ROTULO.search(html)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    zona_leste: list[str] = []
    outros: list[str] = []
    vistos: set[str] = set()
    for m in RE_CEP_BR.finditer(html):
        cep = f"{m.group(1)}-{m.group(2)}"
        if cep in vistos:
            continue
        vistos.add(cep)
        if m.group(1).startswith("08"):
            zona_leste.append(cep)
        else:
            outros.append(cep)
    if zona_leste:
        return zona_leste[0]
    if outros:
        return outros[0]
    return None


def _endereco_parece_marketing(texto: str) -> bool:
    t = texto.lower()
    return (
        "entre em contato" in t
        or "para venda com" in t
        or "por r$" in t
        or "vivareal" in t and "casas na zona" in t
    )


def extrair_endereco_do_html(html: str, soup: BeautifulSoup | None = None) -> str:
    if soup is None:
        soup = BeautifulSoup(html, "html.parser")

    # 1) Linha de rua visível no HTML (padrão Viva Real / Zap)
    m = RE_ENDERECO_LINHA.search(html)
    if m:
        return _limpar_texto_endereco(m.group(1))

    # 2) JSON-LD com PostalAddress
    for block in _iter_ld_json(html):
        end = block.get("address")
        if end is None:
            obj = block.get("object")
            if isinstance(obj, dict):
                end = obj.get("address")
        if isinstance(end, dict):
            txt = _endereco_de_dict(end)
            if txt:
                return txt
        if isinstance(end, str) and end and not _endereco_parece_marketing(end):
            return _limpar_texto_endereco(end)

    # 3) Campos de endereço em JSON embutido (scripts / payload)
    acc: list[str] = []
    for script in soup.find_all("script"):
        raw = script.string or script.get_text() or ""
        if len(raw) < 50:
            continue
        if "address" not in raw.lower() and "street" not in raw.lower():
            continue
        try:
            if raw.strip().startswith(("{", "[")):
                _walk_endereco_json(json.loads(raw), acc)
        except json.JSONDecodeError:
            pass
    m_st = re.search(r'"streetAddress"\s*:\s*"([^"]+)"', html)
    if m_st:
        bairro_m = re.search(r'"neighborhood"\s*:\s*"([^"]+)"', html)
        cidade_m = re.search(r'"city"\s*:\s*"([^"]+)"', html)
        partes = [m_st.group(1)]
        if bairro_m:
            partes.append(bairro_m.group(1))
        if cidade_m:
            partes.append(cidade_m.group(1))
        partes.append("SP")
        acc.insert(0, _limpar_texto_endereco(", ".join(partes)))
    if acc:
        return acc[0]

    # 4) CEP + bairro (quando a rua não vem exposta)
    cep = _extrair_cep_do_html(html)
    if cep:
        for bloco in _iter_ld_json(html):
            if isinstance(bloco.get("address"), dict):
                b = bloco["address"].get("addressLocality") or bloco["address"].get(
                    "neighborhood"
                )
                if b:
                    return _limpar_texto_endereco(f"{b}, São Paulo - SP, CEP {cep}")
        return f"CEP {cep}"

    # 5) Não usar meta description (texto promocional do portal)
    return ""


def ponto_no_bounding_box(lat: float, lon: float, bbox: BoundingBox) -> bool:
    return bbox.sul <= lat <= bbox.norte and bbox.oeste <= lon <= bbox.leste


def texto_menciona_bairro_interesse(texto: str, bairros: list[str]) -> bool:
    blob = (texto or "").lower()
    return any(b.lower() in blob for b in bairros if b)


def avaliar_filtro_geografico(
    lat: float,
    lon: float,
    filtro: FiltroRegioes,
    config: ConfigColeta,
    texto_localizacao: str,
    url: str,
) -> tuple[bool, str]:
    """
    Decide se o anúncio entra no CSV.
    poligono: só dentro dos polígonos desenhados nas imagens.
    bbox: só dentro do bounding_box do JSON.
    poligono_ou_bbox (padrão): polígono OU bounding_box (evita CSV vazio se mapa não calibrar).
    poligono_ou_bairro: polígono OU bbox OU menção a bairro de interesse no texto/URL.
    """
    modo = (config.filtro_geo or "poligono_ou_bbox").lower()

    for reg in filtro.regioes:
        verts = reg.poligono.vertices_latlng
        if len(verts) < 3:
            continue
        anel = [(lng, la) for la, lng in verts]
        if ponto_em_poligono(lon, lat, anel):
            return True, reg.id_regiao

    if modo in ("bbox", "poligono_ou_bbox", "poligono_ou_bairro"):
        if ponto_no_bounding_box(lat, lon, config.bounding_box):
            return True, ""

    if modo == "poligono_ou_bairro":
        blob = f"{texto_localizacao} {url}"
        if texto_menciona_bairro_interesse(blob, config.bairros_interesse):
            return True, ""

    return False, ""


def extrair_titulo(soup: BeautifulSoup) -> str:
    t = soup.find("meta", property="og:title") or soup.find("title")
    if t and t.get("content"):
        return t["content"].strip()
    if t and t.string:
        return t.string.strip()
    return ""


def processar_url_anuncio(
    url: str,
    sessao: Any,
    filtro: FiltroRegioes,
    geolocator: Nominatim,
    config: ConfigColeta,
) -> AnuncioCasa | None:
    url = normalizar_url_anuncio(url)
    if not parece_url_anuncio_individual(url):
        LOG.debug("Ignorada (não é página de anúncio): %s", url[:100])
        return None

    headers: dict[str, str] = {}
    ref = _referer_para_url(url)
    if ref:
        headers["Referer"] = ref

    req_kw: dict[str, Any] = {
        "timeout": config.timeout_http,
        "allow_redirects": True,
    }
    if headers:
        req_kw["headers"] = headers
    def _get_pagina(target: str):
        ref_t = _referer_para_url(target)
        kw = dict(req_kw)
        if ref_t:
            kw["headers"] = {**(headers or {}), "Referer": ref_t}
        return sessao.get(target, **kw)

    try:
        r = _get_pagina(url)
        if r.status_code in (403, 429):
            LOG.info("HTTP %s em %s; aguardando e reaquecendo sessão…", r.status_code, url[:80])
            time.sleep(4.0)
            aquecer_sessao_portais(sessao, config)
            r = _get_pagina(url)
        if r.status_code == 404:
            alt = url_espelho_vivareal(url)
            if alt and alt != url:
                LOG.info("404 no Zap; tentando espelho Viva Real: %s", alt[:100])
                r = _get_pagina(alt)
        if r.status_code == 404:
            LOG.warning(
                "GET %s: HTTP 404 (link antigo da busca ou anúncio encerrado)",
                url[:120],
            )
            return None
        r.raise_for_status()
    except _ERROS_HTTP_SESSAO as e:
        LOG.warning("GET %s: %s", url[:120], e)
        return None

    html = r.text
    soup = BeautifulSoup(html, "html.parser")
    titulo = extrair_titulo(soup)
    texto = _texto_visivel(soup)
    ok_tipo, motivo = _eh_titulo_likely_casa(titulo, texto, url=r.url)
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
        LOG.debug("Descartado (tipo): %s — %s", url[:80], motivo)
        return None

    anuncio.endereco = extrair_endereco_do_html(html, soup=soup)
    anuncio.tamanho_m2 = extrair_m2_do_html(html, texto, titulo, url=r.url)

    phones = extrair_telefones_do_html(html, r.url, soup=soup)
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
        LOG.debug("Descartado (sem coordenadas): %s", url[:80])
        return None

    anuncio.latitude = str(lat)
    anuncio.longitude = str(lon)

    texto_loc = f"{titulo} {anuncio.endereco}"
    aceito, regiao_id = avaliar_filtro_geografico(
        lat, lon, filtro, config, texto_loc, r.url
    )
    if not aceito:
        LOG.debug(
            "Descartado (fora da área de interesse, modo=%s): %s (%.5f, %.5f)",
            config.filtro_geo,
            url[:80],
            lat,
            lon,
        )
        return None

    anuncio.regiao_poligono_id = regiao_id
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

    sessao = criar_sessao_http(config)
    aquecer_sessao_portais(sessao, config)

    urls = coletar_urls(config, sessao)
    LOG.info("%s URLs candidatas após busca (somente páginas de anúncio).", len(urls))
    if not urls:
        LOG.warning(
            "Nenhuma URL de anúncio válida. Confira hubs em urls_paginas_hub ou reduza "
            "dependência só de buscas (links de busca costumam estar desatualizados)."
        )

    limite = max(1, config.max_anuncios_processar)
    if len(urls) > limite:
        LOG.info(
            "Processando os primeiros %s de %s URLs (max_anuncios_processar).",
            limite,
            len(urls),
        )
        urls = urls[:limite]

    geolocator = Nominatim(user_agent=config.user_agent[:120], timeout=20)
    anuncios: list[AnuncioCasa] = []
    for i, u in enumerate(urls, start=1):
        LOG.info("Processando %s/%s …", i, len(urls))
        try:
            a = processar_url_anuncio(u, sessao, filtro, geolocator, config)
        except Exception as e:
            LOG.warning("Erro inesperado em %s: %s", u[:100], e)
            a = None
        if a:
            anuncios.append(a)
            m2_txt = f" | {a.tamanho_m2} m²" if a.tamanho_m2 else ""
            LOG.info("Aceito: %s%s", a.link[:90], m2_txt)
        time.sleep(config.pausa_segundos_entre_requisicoes)

    out = raiz / config.arquivo_saida_csv
    salvar_csv(anuncios, out)
    LOG.info("CSV gravado em %s (%s linhas).", out, len(anuncios))
    try:
        from gerar_mapa import atualizar_site_mapa

        js_mapa = atualizar_site_mapa(out, raiz / "site")
        LOG.info("Mapa do site atualizado: %s", js_mapa)
    except Exception as e:
        LOG.warning("Não foi possível atualizar o mapa em site/: %s", e)
    if len(anuncios) == 0 and urls:
        LOG.warning(
            "Nenhum anúncio passou no filtro. Causas comuns: HTTP 404 em links antigos da "
            "busca, página sem coordenadas, ou imóvel fora dos polígonos em regioes_interesse."
        )
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
