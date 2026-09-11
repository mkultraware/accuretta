The runtime uses the bundled PostCSS parser in `assets/vendor/postcss/parse.cjs`.
No npm install is needed to run checks; the existing trusted Node.js runtime is used.

To rebuild from this directory:

```sh
npm ci --ignore-scripts
node build.cjs
```

Dependencies are pinned in package-lock.json. The bundle includes their MIT licenses.
The checker parses stdin only, disables source maps, and does not load project
configuration, plugins, imports, or referenced resources. It checks CSS syntax,
not property-value validity, browser compatibility, or rendering.
