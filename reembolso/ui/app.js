// Comportamentos locais da interface. Nenhum recurso externo é carregado.
(function () {
  // Cronômetro do painel "Analisando…": atualiza todo elemento com data-ra-inicio (epoch em ms).
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

  // Tema: claro, escuro ou automático (segue o sistema). O Gradio usa a classe "dark" no <body>.
  const CHAVE = "ra-tema";
  const sistema = window.matchMedia("(prefers-color-scheme: dark)");
  function lerTema() {
    try { return localStorage.getItem(CHAVE) || "auto"; } catch (e) { return "auto"; }
  }
  function aplicar(tema) {
    const escuro = tema === "escuro" || (tema === "auto" && sistema.matches);
    document.body.classList.toggle("dark", escuro);
    document.querySelectorAll("[data-ra-tema]").forEach(function (b) {
      b.setAttribute("aria-pressed", String(b.getAttribute("data-ra-tema") === tema));
    });
  }
  document.addEventListener("click", function (ev) {
    const b = ev.target.closest && ev.target.closest("[data-ra-tema]");
    if (!b) return;
    const tema = b.getAttribute("data-ra-tema");
    try { localStorage.setItem(CHAVE, tema); } catch (e) { /* armazenamento indisponível */ }
    aplicar(tema);
  });
  sistema.addEventListener("change", function () { if (lerTema() === "auto") aplicar("auto"); });
  // O Gradio monta a página depois do <head>: reaplica quando o rodapé aparecer.
  const obs = new MutationObserver(function () {
    if (document.querySelector("[data-ra-tema]")) { aplicar(lerTema()); obs.disconnect(); }
  });
  obs.observe(document.documentElement, { childList: true, subtree: true });
})();
