"""
filtro_regioes.py
-----------------
Analisa imagens (mapas, ex.: capturas do Google Maps) na pasta `regioes_interesse`,
detecta regiões demarcadas por contornos vermelhos ou amarelos/amarelo‑verdes e
monta um filtro geográfico reutilizável para o web scraping (CEP/endereço → ponto → polígono).

Requisitos:
    pip install numpy opencv-python-headless

Nota: imagens raster não contêm coordenadas geográficas embutidas. Os polígonos são extraídos
em coordenadas normalizadas da imagem (0–1). Para obter lat/lng é obrigatório informar o
limites do mapa visível (`BoundingBox`) alinhado à mesma área e zoom do recorte — por exemplo,
medição dos cantos NW/SE no Google Maps ou uso de um georreferenciamento manual.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal, Sequence

import cv2
import numpy as np

CorMarcador = Literal["vermelho", "amarelo"]

# Extensões tratadas ao varrer a pasta
EXTENSOES_IMAGEM = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


@dataclass(frozen=True)
class BoundingBox:
    """Limites geográficos do retângulo do mapa correspondente à imagem (mesma ordem de visualização)."""

    sul: float   # latitude mínima (borda inferior da imagem)
    oeste: float # longitude mínima (borda esquerda)
    norte: float # latitude máxima (borda superior)
    leste: float # longitude máxima (borda direita)

    def __post_init__(self) -> None:
        if not (-90 <= self.sul <= 90 and -90 <= self.norte <= 90):
            raise ValueError("latitudes devem estar entre -90 e 90")
        if self.sul >= self.norte:
            raise ValueError("sul deve ser menor que norte (latitude aumenta para o norte)")
        if not (-180 <= self.oeste <= 180 and -180 <= self.leste <= 180):
            raise ValueError("longitudes devem estar entre -180 e 180")
        if self.oeste >= self.leste:
            raise ValueError("oeste deve ser menor que leste")


@dataclass
class PoligonoRegiao:
    """Polígono em uma única representação; use apenas um dos campos preenchidos por etapa do fluxo."""

    vertices_normalizados: list[tuple[float, float]] = field(default_factory=list)
    """(x, y) com x,y ∈ [0, 1] relativos à largura/altura da imagem de origem (origem no canto superior esquerdo)."""

    vertices_latlng: list[tuple[float, float]] = field(default_factory=list)
    """(latitude, longitude) em graus decimais, após aplicar `BoundingBox`."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "vertices_normalizados": [list(v) for v in self.vertices_normalizados],
            "vertices_latlng": [list(v) for v in self.vertices_latlng],
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> PoligonoRegiao:
        vn = [tuple(map(float, v)) for v in data.get("vertices_normalizados", [])]
        vl = [tuple(map(float, v)) for v in data.get("vertices_latlng", [])]
        return PoligonoRegiao(vertices_normalizados=vn, vertices_latlng=vl)


@dataclass
class RegiaoDetectada:
    id_regiao: str
    arquivo_origem: str
    cor_marcador: CorMarcador
    poligono: PoligonoRegiao
    area_pixels: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "id_regiao": self.id_regiao,
            "arquivo_origem": self.arquivo_origem,
            "cor_marcador": self.cor_marcador,
            "poligono": self.poligono.to_dict(),
            "area_pixels": self.area_pixels,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> RegiaoDetectada:
        return RegiaoDetectada(
            id_regiao=str(d["id_regiao"]),
            arquivo_origem=str(d["arquivo_origem"]),
            cor_marcador=d["cor_marcador"],  # type: ignore[arg-type]
            poligono=PoligonoRegiao.from_dict(d["poligono"]),
            area_pixels=float(d["area_pixels"]),
        )


def _mascara_vermelho_hsv(hsv: np.ndarray) -> np.ndarray:
    """Máscara para traços vermelhos (HSV com dois intervalos no matiz)."""
    lower1 = np.array([0, 80, 80], dtype=np.uint8)
    upper1 = np.array([12, 255, 255], dtype=np.uint8)
    lower2 = np.array([168, 80, 80], dtype=np.uint8)
    upper2 = np.array([180, 255, 255], dtype=np.uint8)
    m1 = cv2.inRange(hsv, lower1, upper1)
    m2 = cv2.inRange(hsv, lower2, upper2)
    return cv2.bitwise_or(m1, m2)


def _mascara_amarelo_hsv(hsv: np.ndarray) -> np.ndarray:
    """Máscara para traços amarelos / amarelo‑verdes (contornos tipo marca‑texto no mapa)."""
    lower = np.array([18, 80, 80], dtype=np.uint8)
    upper = np.array([45, 255, 255], dtype=np.uint8)
    return cv2.inRange(hsv, lower, upper)


def _refinar_mascara_binaria(mask: np.ndarray) -> np.ndarray:
    """Fecha pequenas falhas nos traços e liga segmentos do contorno."""
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    m = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k, iterations=2)
    m = cv2.dilate(m, k, iterations=1)
    return m


def _contornos_para_poligonos(
    mask: np.ndarray,
    min_area_pixels: float,
    epsilon_frac: float,
) -> tuple[list[np.ndarray], list[float]]:
    contours, _h = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    polys: list[np.ndarray] = []
    areas: list[float] = []
    for cnt in contours:
        area = float(cv2.contourArea(cnt))
        if area < min_area_pixels:
            continue
        peri = cv2.arcLength(cnt, True)
        eps = max(epsilon_frac * peri, 1.0)
        approx = cv2.approxPolyDP(cnt, eps, True)
        if len(approx) < 3:
            continue
        polys.append(approx.reshape(-1, 2))
        areas.append(area)
    return polys, areas


def _normalizar_vertices(
    verts_xy: np.ndarray, largura: int, altura: int
) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for x, y in verts_xy:
        nx = float(x) / largura
        ny = float(y) / altura
        nx = min(1.0, max(0.0, nx))
        ny = min(1.0, max(0.0, ny))
        out.append((nx, ny))
    return out


def normalizado_para_latlng(
    x_norm: float, y_norm: float, bbox: BoundingBox
) -> tuple[float, float]:
    """
    Converte (x_norm, y_norm) da imagem para (lat, lng).
    y_norm=0 corresponde à borda norte (latitude máxima); x_norm=0 ao oeste.
    """
    lat = bbox.norte - y_norm * (bbox.norte - bbox.sul)
    lng = bbox.oeste + x_norm * (bbox.leste - bbox.oeste)
    return lat, lng


def poligono_normalizado_para_latlng(
    vertices: Sequence[tuple[float, float]], bbox: BoundingBox
) -> list[tuple[float, float]]:
    return [normalizado_para_latlng(x, y, bbox) for x, y in vertices]


def ponto_em_poligono(lon: float, lat: float, anel_lonlat: Sequence[tuple[float, float]]) -> bool:
    """Ray casting; anel_lonlat como (longitude, latitude) por vértice — ordem fechada."""
    n = len(anel_lonlat)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = anel_lonlat[i]
        xj, yj = anel_lonlat[j]
        intersect = (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi + 1e-15) + xi
        if intersect:
            inside = not inside
        j = i
    return inside


class FiltroRegioes:
    """
    Conjunto de regiões detectadas. Use `aplicar_bbox` depois de definir o retângulo do mapa
    para preencher `vertices_latlng` e habilitar `contem_coordenadas`.
    """

    def __init__(self, regioes: Sequence[RegiaoDetectada]):
        self.regioes = list(regioes)

    def aplicar_bbox(self, bbox: BoundingBox) -> None:
        for reg in self.regioes:
            reg.poligono.vertices_latlng = poligono_normalizado_para_latlng(
                reg.poligono.vertices_normalizados, bbox
            )

    def aplicar_bbox_por_imagem(
        self,
        bbox_por_arquivo: dict[str, BoundingBox],
        bbox_padrao: BoundingBox | None = None,
    ) -> None:
        """
        Aplica um `BoundingBox` distinto por arquivo de imagem de origem (recomendado quando
        cada captura de mapa tem zoom/posição diferentes). Chaves de `bbox_por_arquivo` devem
        coincidir com `RegiaoDetectada.arquivo_origem` (nome do ficheiro).
        """
        for reg in self.regioes:
            bbox = bbox_por_arquivo.get(reg.arquivo_origem) or bbox_padrao
            if bbox is None:
                raise ValueError(
                    f"Sem BoundingBox para a imagem {reg.arquivo_origem!r}. "
                    "Inclua essa chave em bbox_por_arquivo ou defina bounding_box como padrão."
                )
            reg.poligono.vertices_latlng = poligono_normalizado_para_latlng(
                reg.poligono.vertices_normalizados, bbox
            )

    def contem_coordenadas(self, latitude: float, longitude: float) -> bool:
        """Verdadeiro se o ponto cair em qualquer polígono (usa `vertices_latlng`)."""
        for reg in self.regioes:
            verts = reg.poligono.vertices_latlng
            if len(verts) < 3:
                continue
            anel = [(lng, lat) for lat, lng in verts]
            if ponto_em_poligono(longitude, latitude, anel):
                return True
        return False

    def contem_coordenadas_qualquer_bbox(
        self, latitude: float, longitude: float, bbox: BoundingBox
    ) -> bool:
        """Útil quando o objeto ainda não chamou `aplicar_bbox`; projeta na hora."""
        for reg in self.regioes:
            verts_ll = poligono_normalizado_para_latlng(reg.poligono.vertices_normalizados, bbox)
            if len(verts_ll) < 3:
                continue
            anel = [(lng, lat) for lat, lng in verts_ll]
            if ponto_em_poligono(longitude, latitude, anel):
                return True
        return False

    def salvar_json(self, caminho: str | Path) -> None:
        p = Path(caminho)
        payload = {
            "versao": 1,
            "regioes": [r.to_dict() for r in self.regioes],
        }
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def carregar_json(caminho: str | Path) -> FiltroRegioes:
        data = json.loads(Path(caminho).read_text(encoding="utf-8"))
        regioes = [RegiaoDetectada.from_dict(r) for r in data.get("regioes", [])]
        return FiltroRegioes(regioes)

    def resumo(self) -> str:
        linhas = [f"{len(self.regioes)} região(ões) detectada(s):"]
        for r in self.regioes:
            n = len(r.poligono.vertices_normalizados)
            linhas.append(f"  - {r.id_regiao} | {r.cor_marcador} | {r.arquivo_origem} | {n} vértices")
        return "\n".join(linhas)


def _escolher_cor_por_mascara(
    imagem_bgr: np.ndarray,
    min_area: float,
    epsilon_frac: float,
) -> tuple[CorMarcador, list[tuple[np.ndarray, float]]]:
    hsv = cv2.cvtColor(imagem_bgr, cv2.COLOR_BGR2HSV)
    h, w = imagem_bgr.shape[:2]
    area_img = float(h * w)
    min_area_eff = max(min_area, 0.00012 * area_img)

    resultados: list[tuple[CorMarcador, list[tuple[np.ndarray, float]]]] = []
    for cor, mascara_fn in (
        ("vermelho", _mascara_vermelho_hsv),
        ("amarelo", _mascara_amarelo_hsv),
    ):
        m = mascara_fn(hsv)
        m = _refinar_mascara_binaria(m)
        polys, areas = _contornos_para_poligonos(m, min_area_eff, epsilon_frac)
        if polys:
            resultados.append((cor, list(zip(polys, areas))))  # type: ignore[arg-type]

    if not resultados:
        return "vermelho", []

    # Prioriza o marcador que gerou mais “área de traço” acumulada (evita fundo confundir uma cor).
    def score(item: tuple[CorMarcador, list[tuple[np.ndarray, float]]]) -> float:
        _c, pairs = item
        return sum(a for _p, a in pairs)

    resultados.sort(key=score, reverse=True)
    melhor_cor, pares = resultados[0]
    return melhor_cor, pares


def extrair_regioes_da_imagem(
    caminho_imagem: str | Path,
    *,
    min_area_pixels: float = 450.0,
    epsilon_frac: float = 0.01,
    prefixo_id: str = "",
) -> list[RegiaoDetectada]:
    """
    Detecta polígonos fechados delimitados por traço vermelho ou amarelo na imagem.

    Parâmetros de tunagem:
    - min_area_pixels: área mínima do contorno em pixels (ajuste se faltar/sobrar região).
    - epsilon_frac: fração do perímetro para simplificação (maiores = polígonos com menos vértices).
    """
    path = Path(caminho_imagem)
    img = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise FileNotFoundError(f"Não foi possível ler a imagem: {path}")

    h, w = img.shape[:2]
    cor, pares = _escolher_cor_por_mascara(img, min_area_pixels, epsilon_frac)
    regioes: list[RegiaoDetectada] = []
    base = prefixo_id or path.stem
    for idx, (verts, area) in enumerate(pares):
        vn = _normalizar_vertices(verts, w, h)
        rid = f"{base}_{idx + 1}"
        regioes.append(
            RegiaoDetectada(
                id_regiao=rid,
                arquivo_origem=path.name,
                cor_marcador=cor,
                poligono=PoligonoRegiao(vertices_normalizados=vn, vertices_latlng=[]),
                area_pixels=area,
            )
        )
    return regioes


def extrair_regioes_da_pasta(
    pasta: str | Path | None = None,
    *,
    min_area_pixels: float = 450.0,
    epsilon_frac: float = 0.01,
) -> FiltroRegioes:
    """
    Varre `pasta` (padrão: `regioes_interesse` ao lado deste arquivo) e agrega todas as regiões.
    """
    base = Path(__file__).resolve().parent
    raiz = Path(pasta) if pasta is not None else base / "regioes_interesse"
    if not raiz.is_dir():
        raise NotADirectoryError(f"Pasta não encontrada: {raiz}")

    todas: list[RegiaoDetectada] = []
    arquivos = sorted(
        p for p in raiz.iterdir() if p.is_file() and p.suffix.lower() in EXTENSOES_IMAGEM
    )
    for arq in arquivos:
        extras = extrair_regioes_da_imagem(
            arq, min_area_pixels=min_area_pixels, epsilon_frac=epsilon_frac, prefixo_id=arq.stem
        )
        todas.extend(extras)

    return FiltroRegioes(todas)


def construir_filtro(
    pasta: str | Path | None = None,
    bbox: BoundingBox | None = None,
    **kwargs: Any,
) -> FiltroRegioes:
    """
    Atalho: extrai regiões da pasta e opcionalmente aplica `BoundingBox` geográfico.
    """
    filtro = extrair_regioes_da_pasta(pasta, **kwargs)
    if bbox is not None:
        filtro.aplicar_bbox(bbox)
    return filtro


__all__ = [
    "BoundingBox",
    "PoligonoRegiao",
    "RegiaoDetectada",
    "FiltroRegioes",
    "construir_filtro",
    "extrair_regioes_da_pasta",
    "extrair_regioes_da_imagem",
    "normalizado_para_latlng",
    "poligono_normalizado_para_latlng",
    "ponto_em_poligono",
]


if __name__ == "__main__":
    import sys

    out_json = Path(__file__).resolve().parent / "filtro_regioes_gerado.json"
    try:
        f = extrair_regioes_da_pasta()
    except Exception as e:
        print("Erro ao processar imagens:", e, file=sys.stderr)
        sys.exit(1)

    print(f.resumo())
    f.salvar_json(out_json)
    print(f"\nArquivo salvo em: {out_json}")
    print(
        "\nPróximo passo: defina um BoundingBox (sul, oeste, norte, leste) alinhado ao recorte "
        "do mapa e chame filtro.aplicar_bbox(bbox) antes de usar contem_coordenadas com lat/lng, "
        "ou use contem_coordenadas_qualquer_bbox(lat, lng, bbox)."
    )
