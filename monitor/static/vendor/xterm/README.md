# Vendored xterm.js

The Web terminal ships these browser dependencies locally so an intranet
deployment does not need a CDN or an npm build step:

- `@xterm/xterm` 5.5.0
- `@xterm/addon-fit` 0.10.0

Both packages use the MIT license. Their license texts are stored as
`LICENSE.xterm` and `LICENSE.addon-fit` in this directory.

When upgrading, replace the JavaScript, CSS and license files together, then
run the Python test suite, `node --check monitor/static/app.js`, the real
WebSocket acceptance test and the browser terminal regression.
