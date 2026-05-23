/* global L, IMOVEIS_DATA */

(function () {
  "use strict";

  const CENTRO_SP = [-23.55, -46.63];
  const ZOOM_INICIAL = 12;

  function criarIconePin() {
    return L.divIcon({
      className: "pin-moderno-wrap",
      html:
        '<div class="pin-moderno" aria-hidden="true">' +
        '<span class="pin-moderno-ponto"></span>' +
        '<span class="pin-moderno-sombra"></span>' +
        "</div>",
      iconSize: [40, 48],
      iconAnchor: [20, 46],
      popupAnchor: [0, -44],
      tooltipAnchor: [20, -40],
    });
  }

  function escHtml(texto) {
    const d = document.createElement("div");
    d.textContent = texto || "";
    return d.innerHTML;
  }

  function atualizarMeta(dados) {
    const el = document.getElementById("meta-atualizacao");
    if (!el) return;
    const quando = dados.atualizado_em
      ? new Date(dados.atualizado_em).toLocaleString("pt-BR")
      : "—";
    el.innerHTML =
      `<span class="meta-total">${dados.total} imóvel${dados.total === 1 ? "" : "is"} no mapa</span>` +
      `<span class="meta-data">Atualizado: ${escHtml(quando)}</span>`;
  }

  function conteudoTooltip(imovel) {
    const titulo = escHtml(imovel.titulo || "Casa à venda");
    const link = escHtml(imovel.link);
    return (
      `<strong>${titulo}</strong><br>` +
      `<a href="${link}" target="_blank" rel="noopener noreferrer">Abrir anúncio ↗</a>`
    );
  }

  function conteudoPopup(imovel) {
    const partes = [];
    if (imovel.endereco) {
      partes.push(`<p class="endereco">${escHtml(imovel.endereco)}</p>`);
    }
    if (imovel.tamanho_m2) {
      partes.push(`<p class="detalhe">${escHtml(imovel.tamanho_m2)} m²</p>`);
    }
    if (imovel.telefone_imobiliaria) {
      partes.push(
        `<p class="detalhe">Tel. imob.: ${escHtml(imovel.telefone_imobiliaria)}</p>`
      );
    }
    partes.push(
      `<a class="link-anuncio" href="${escHtml(imovel.link)}" target="_blank" rel="noopener noreferrer">Ver anúncio no site</a>`
    );
    return `<div class="pin-popup">${partes.join("")}</div>`;
  }

  function boundsDosImoveis(imoveis) {
    if (!imoveis.length) return null;
    const latlngs = imoveis.map((i) => [i.lat, i.lng]);
    return L.latLngBounds(latlngs);
  }

  function iniciarMapa() {
    const dados = window.IMOVEIS_DATA;
    const container = document.getElementById("mapa");

    if (!dados || !Array.isArray(dados.imoveis)) {
      container.innerHTML =
        '<div class="mensagem-vazia"><p>Não foi possível carregar <code>dados.js</code>. Rode <code>python gerar_mapa.py</code> após o coletor.</p></div>';
      return;
    }

    atualizarMeta(dados);

    if (dados.imoveis.length === 0) {
      container.innerHTML =
        '<div class="mensagem-vazia"><p>Nenhum imóvel com latitude/longitude no CSV.<br>Rode o coletor e depois <code>python gerar_mapa.py</code>.</p></div>';
      return;
    }

    const mapa = L.map("mapa", { scrollWheelZoom: true }).setView(CENTRO_SP, ZOOM_INICIAL);

    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      attribution:
        '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
      maxZoom: 19,
    }).addTo(mapa);

    const grupo = L.layerGroup().addTo(mapa);

    dados.imoveis.forEach((imovel) => {
      const marker = L.marker([imovel.lat, imovel.lng], { icon: criarIconePin() });

      marker.bindTooltip(conteudoTooltip(imovel), {
        className: "pin-tooltip",
        direction: "top",
        offset: [0, -36],
        opacity: 1,
      });

      marker.bindPopup(conteudoPopup(imovel), { maxWidth: 320 });

      marker.on("mouseover", function () {
        const el = this.getElement();
        if (el) el.classList.add("pin-moderno--hover");
        this.openTooltip();
        this.setZIndexOffset(1000);
      });
      marker.on("mouseout", function () {
        const el = this.getElement();
        if (el) el.classList.remove("pin-moderno--hover");
        this.closeTooltip();
        this.setZIndexOffset(0);
      });

      marker.addTo(grupo);
    });

    const bounds = boundsDosImoveis(dados.imoveis);
    if (bounds && bounds.isValid()) {
      mapa.fitBounds(bounds.pad(0.12));
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", iniciarMapa);
  } else {
    iniciarMapa();
  }
})();
