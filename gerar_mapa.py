"""
gerar_mapa.py
-------------
Lê o CSV do coletor e gera `site/dados.js`, usado pelo mapa estático em `site/index.html`.

O site é apenas reflexo visual da base: sempre que o CSV mudar, rode este script
(ou use o coletor, que já chama a atualização automaticamente).

Uso:
    python gerar_mapa.py
    python gerar_mapa.py --entrada saida/casas_filtradas.csv --pasta-site site
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path


RE_CEP = re.compile(r"CEP\s*(\d{5})-?(\d{3})", re.IGNORECASE)


def _cep_do_endereco(endereco: str) -> str | None:
    m = RE_CEP.search(endereco or "")
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    return None


def _float_ou_none(valor: str) -> float | None:
    valor = (valor or "").strip().replace(",", ".")
    if not valor:
        return None
    try:
        return float(valor)
    except ValueError:
        return None


def ler_imoveis_do_csv(caminho_csv: Path) -> list[dict]:
    if not caminho_csv.is_file():
        return []

    imoveis: list[dict] = []
    with caminho_csv.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for idx, row in enumerate(reader, start=1):
            lat = _float_ou_none(row.get("latitude", ""))
            lon = _float_ou_none(row.get("longitude", ""))
            if lat is None or lon is None:
                continue

            endereco = (row.get("endereco") or "").strip()
            link = (row.get("link") or "").strip()
            if not link.startswith("http"):
                continue

            titulo = (row.get("titulo") or "").strip()
            if not titulo or "Viva Real" in titulo[-20:]:
                titulo = endereco.split(",")[0] if endereco else f"Imóvel {idx}"

            imoveis.append(
                {
                    "id": idx,
                    "lat": lat,
                    "lng": lon,
                    "endereco": endereco,
                    "link": link,
                    "tamanho_m2": (row.get("tamanho_m2") or "").strip(),
                    "telefone_imobiliaria": (row.get("telefone_imobiliaria") or "").strip(),
                    "telefone_vendedor": (row.get("telefone_vendedor") or "").strip(),
                    "titulo": titulo[:120],
                    "cep": _cep_do_endereco(endereco) or "",
                }
            )
    return imoveis


def atualizar_site_mapa(
    caminho_csv: Path,
    pasta_site: Path | None = None,
) -> Path:
    """
    Gera `dados.js` na pasta do site. Retorna o caminho do arquivo gerado.
    """
    raiz = Path(__file__).resolve().parent
    pasta = pasta_site or (raiz / "site")
    pasta.mkdir(parents=True, exist_ok=True)

    caminho_csv = Path(caminho_csv).resolve()
    imoveis = ler_imoveis_do_csv(caminho_csv)

    payload = {
        "atualizado_em": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "fonte_csv": str(caminho_csv.name),
        "total": len(imoveis),
        "imoveis": imoveis,
    }

    saida_js = pasta / "dados.js"
    conteudo = (
        "// Gerado automaticamente por gerar_mapa.py — não edite à mão.\n"
        f"window.IMOVEIS_DATA = {json.dumps(payload, ensure_ascii=False, indent=2)};\n"
    )
    saida_js.write_text(conteudo, encoding="utf-8")
    return saida_js


def main() -> None:
    raiz = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(
        description="Atualiza o mapa estático (site/) a partir do CSV de casas."
    )
    ap.add_argument(
        "--entrada",
        default=str(raiz / "saida" / "casas_filtradas.csv"),
        help="CSV gerado pelo coletor",
    )
    ap.add_argument(
        "--pasta-site",
        default=str(raiz / "site"),
        help="Pasta do site estático",
    )
    args = ap.parse_args()

    caminho_csv = Path(args.entrada)
    saida = atualizar_site_mapa(caminho_csv, Path(args.pasta_site))
    imoveis = ler_imoveis_do_csv(caminho_csv)
    print(f"Mapa atualizado: {saida}")
    print(f"Imóveis no mapa: {len(imoveis)}")
    print(f"Abra no navegador: {(Path(args.pasta_site) / 'index.html').resolve()}")


if __name__ == "__main__":
    main()
