# projeto_imangai

Projeto de web scraping para encontrar na internet possíveis casas à venda nas regiões de interesse enviadas.

## Passo 1 — desenvolvendo filtro para regiões de interesse

- Foi criado o módulo **`filtro_regioes.py`** na raiz do projeto.
- O script **varre imagens** na pasta **`regioes_interesse`** e detecta **contornos vermelhos ou amarelos/amarelo-verdes** desenhados sobre o mapa (visão computacional com OpenCV, espaço de cor HSV).
- Cada contorno fechado vira um **polígono** com vértices em **coordenadas normalizadas** (0 a 1 em relação à largura e à altura da imagem), pois arquivos de imagem **não trazem latitude/longitude** embutidas.
- A classe **`FiltroRegioes`** permite, depois, aplicar um **`BoundingBox`** (sul, oeste, norte, leste) alinhado ao recorte do mapa, converter os polígonos para **lat/lng** e testar se um ponto (por exemplo, vindo de um CEP geocodificado) **cai dentro** de alguma região.
- Há **serialização em JSON** (`salvar_json` / `carregar_json`) para reutilizar o filtro na etapa de scraping.
- Dependências para rodar o filtro: `numpy` e `opencv-python-headless` (ver docstring no topo de `filtro_regioes.py`).

Próximos passos previstos: geocodificar endereços e integrar o filtro ao coletor de anúncios de casas.
