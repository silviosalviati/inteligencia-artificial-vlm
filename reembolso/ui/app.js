// Cronômetro do painel "Analisando…": atualiza todo elemento com data-ra-inicio (epoch em ms).
// Tudo local: nenhum recurso externo é carregado.
(function () {
  function fmt(ms) {
    const s = Math.max(0, Math.floor(ms / 1000));
    const m = Math.floor(s / 60);
    return String(m).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0");
  }
  setInterval(function () {
    document.querySelectorAll("[data-ra-inicio]").forEach(function (el) {
      const ini = Number(el.getAttribute("data-ra-inicio"));
      if (ini) el.textContent = fmt(Date.now() - ini);
    });
  }, 1000);
})();
