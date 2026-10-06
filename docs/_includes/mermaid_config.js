(function () {
  /* Evaluated inside just-the-docs' mermaid module script (components/mermaid.html), where `mermaid` is the imported module. Keep the diagram sources so the theme toggle in head_custom.html can re-render them with the other palette. */
  window.adaptMermaid = mermaid;
  document.querySelectorAll('.language-mermaid').forEach(function (el) {
    if (!el.hasAttribute('data-adapt-mermaid-src')) {
      el.setAttribute('data-adapt-mermaid-src', el.textContent);
    }
  });
  return window.adaptMermaidConfig ? window.adaptMermaidConfig() : {{ site.mermaid | jsonify }};
})()
