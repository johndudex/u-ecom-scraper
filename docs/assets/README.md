# Vendored renderer assets

Served same-origin at `/docs/assets/<file>` (whitelisted in
`webapp/scraper/views.py:_serve_doc_asset`) so the spec docs pages load
zero third-party scripts, and the AsyncAPI component's shadow-root
`@import 'assets/default.min.css'` (a path RELATIVE TO THE PAGE, which
ignores the `cssImport` attribute) resolves to a real `text/css`
response instead of an HTML 404 page.

Update by re-downloading and bumping the version here + in views.py:

| file | package | version |
|------|---------|---------|
| swagger-ui-bundle.js, swagger-ui.css | swagger-ui-dist | 5.32.14 |
| asyncapi-web-component.js | @asyncapi/web-component | 3.1.6 |
| default.min.css | @asyncapi/react-component | 3.1.6 |

(note: @asyncapi/web-component 3.1.6's package.json `main` points at a
nonexistent `lib/index.js`; the real bundle is `lib/asyncapi-web-component.js`)
