# projeto_imangai

Projeto de web scraping para encontrar na internet possíveis casas à venda nas regiões de interesse enviadas.

## Passo 1 — desenvolvendo filtro para regiões de interesse

- Foi criado o módulo **`filtro_regioes.py`** na raiz do projeto.
- O script **varre imagens** na pasta **`regioes_interesse`** e detecta **contornos vermelhos ou amarelos/amarelo-verdes** desenhados sobre o mapa (visão computacional com OpenCV, espaço de cor HSV).
- Cada contorno fechado vira um **polígono** com vértices em **coordenadas normalizadas** (0 a 1 em relação à largura e à altura da imagem), pois arquivos de imagem **não trazem latitude/longitude** embutidas.
- A classe **`FiltroRegioes`** permite, depois, aplicar um **`BoundingBox`** (sul, oeste, norte, leste) alinhado ao recorte do mapa, converter os polígonos para **lat/lng** e testar se um ponto (por exemplo, vindo de um CEP geocodificado) **cai dentro** de alguma região.
- Para **várias capturas** com zoom ou centro diferentes, use **`aplicar_bbox_por_imagem`**, que associa um retângulo geográfico ao **nome de cada arquivo** de imagem (coincide com `arquivo_origem` nas regiões detectadas).
- Há **serialização em JSON** (`salvar_json` / `carregar_json`) para reutilizar o filtro na etapa de scraping.
- Dependências: ver **`requirements.txt`** (módulos usados pelo Passo 1: `numpy`, `opencv-python-headless`).

## Passo 2 — coleta na web com filtro geográfico estrito

### 2.1 Arquivo principal e configuração

- Foi criado o módulo **`coletor_casas.py`**, que encadeia **busca de URLs**, **download das páginas**, **extração** de campos e **filtro** pelos polígonos definidos no Passo 1.
- A configuração fica em um JSON (modelo: **`config_coleta.exemplo.json`**): bounding box(es), consultas de busca, pasta de saída, limites de links, pausa entre requisições, domínios permitidos e (opcional) **Google Programmable Search** (Custom Search JSON API) para ampliar a web além do DuckDuckGo.
- **`bounding_boxes_por_imagem`** (recomendado): um retângulo geográfico **por arquivo** em `regioes_interesse`, com os **mesmos cantos** visíveis na captura usada no Passo 1. Isso maximiza a precisão quando há mais de um mapa. O campo **`bounding_box`** continua como **padrão** para qualquer imagem não listada no mapa.
- O **`BoundingBox` deve ser medido no mesmo mapa e recorte** das imagens (ex.: canto superior esquerdo = noroeste, inferior direito = sudoeste/leste).

### 2.2 Busca ampla (não só um portal)

- **DuckDuckGo (HTML)** obtém links a partir das **`queries_busca`** (livres; podem incluir `site:domínio` ou termos amplos).
- Opcionalmente, com **`google_cse_api_key`** e **`google_cse_cx`**, o mesmo conjunto de queries é repetido na **API Custom Search**, o que ajuda a cobrir mais resultados indexados respeitando a política do Google.
- **`dominios_permitidos`** reduz ruído (lista vazia = aceitar qualquer domínio retornado pela busca).

### 2.3 Critérios de imóvel e filtro espacial

- **Somente casa / sobrado (heurística em texto):** títulos e texto da página com termos de **apartamento, terreno, cobertura, loja**, etc. tendem a ser **descartados** antes do filtro geográfico.
- **Filtro estrito:** só entram anúncios para os quais existe **par lat/lng** obtido de metadados/JSON-LD na página **ou** de **geocodificação** (Nominatim) do endereço inferido. Em seguida exige-se que o ponto esteja **dentro de algum polígono** já projetado em lat/lng (via `BoundingBox` por imagem ou único).
- Se não houver coordenada confiável nem geocodificação bem-sucedida, o anúncio **não** entra no CSV (evita falsos positivos fora das regiões).

### 2.4 Dados gravados (CSV)

- Arquivo configurável (padrão **`saida/casas_filtradas.csv`**, UTF-8 com BOM para Excel).
- Colunas: **endereço**, **tamanho (m²)** quando encontrável no HTML, **link**, **telefone da imobiliária**, **telefone do vendedor** (segundo número distinto, quando existir), **site de origem**, **id do polígono** correspondente, **latitude**, **longitude**, **título** da página e campo auxiliar de **motivo de exclusão** (preenchido só em fluxos futuros que exportem rejeitados).

### 2.5 Execução e dependências

- Instalar dependências: `pip install -r requirements.txt`
- Rodar: `python coletor_casas.py --config config_coleta.exemplo.json` (copie o JSON e ajuste chaves antes, se quiser outro nome.)
- **Limitações:** muitos portais usam **Cloudflare** ou anti-bot; em IP residencial o `requests` pode funcionar — se não, será preciso trocar só a camada de download (ex. navegador automatizado) mantendo o filtro e o CSV.
- **Uso responsável:** respeitar termos de uso dos sites, robots.txt e carga dos serviços (Nominatim exige uso moderado; há pausa configurável entre chamadas).

Próximos passos possíveis: enriquecer parsers por domínio, deduplicação por endereço normalizado e exportação direta para planilha (o CSV já abre em Excel/LibreOffice).
