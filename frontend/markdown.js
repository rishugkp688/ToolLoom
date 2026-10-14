/* Locally bundled parser; raw HTML and automatic image requests are disabled. */
(function (root) {
  "use strict";
  const factory = typeof module === "object" && module.exports
    ? require("./vendor/markdown-it.min.js") : root.markdownit;
  const escape = text => String(text).replace(/[&<>"']/g, ch => ({
    "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"
  }[ch]));
  const parser = factory ? factory({html:false, breaks:false, linkify:false, typographer:false}) : null;
  if (parser) {
    const validate = parser.validateLink.bind(parser);
    parser.validateLink = url => validate(url) && /^(?:https?:|mailto:|\/(?!\/)|#)/i.test(url);
    parser.renderer.rules.link_open = (tokens, index, options, env, renderer) => {
      tokens[index].attrSet("target", "_blank");
      tokens[index].attrSet("rel", "noopener noreferrer");
      return renderer.renderToken(tokens, index, options);
    };
    parser.renderer.rules.image = (tokens, index) => `<span class="markdown-image">[Image: ${escape(tokens[index].content || "attachment")}]</span>`;
    parser.renderer.rules.table_open = () => '<div class="markdown-table" role="region" aria-label="Table" tabindex="0"><table>\n';
    parser.renderer.rules.table_close = () => '</table></div>\n';
  }
  function renderMarkdown(source) {
    const text = String(source ?? "");
    return parser ? parser.render(text) : `<p>${escape(text).replace(/\n/g,"<br>")}</p>`;
  }
  if (typeof module === "object" && module.exports) module.exports = renderMarkdown;
  else root.renderMarkdown = renderMarkdown;
})(globalThis);
