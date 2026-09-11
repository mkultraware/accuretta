'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { createHash } = require('node:crypto');
const esbuild = require('esbuild');
const destination = path.resolve(__dirname, '../../assets/vendor/postcss');

fs.mkdirSync(destination, { recursive: true });
esbuild.buildSync({
  absWorkingDir: path.resolve(__dirname, '../..'),
  entryPoints: [require.resolve('postcss/lib/parse')],
  outfile: path.join(destination, 'parse.cjs'),
  bundle: true,
  platform: 'node',
  format: 'cjs',
  target: 'node18',
  legalComments: 'eof',
});
for (const name of ['postcss', 'nanoid', 'picocolors', 'source-map-js']) {
  const directory = path.join(__dirname, 'node_modules', name);
  fs.copyFileSync(path.join(directory, 'LICENSE'), path.join(destination, `${name}-LICENSE.txt`));
}

const manifestPath = path.join(destination, '../manifest.json');
const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'))
  .filter(entry => !entry.file.startsWith('postcss/'));
for (const filename of fs.readdirSync(destination).sort()) {
  const name = filename === 'parse.cjs' ? 'postcss' : filename.replace('-LICENSE.txt', '');
  const { version } = require(path.join(__dirname, 'node_modules', name, 'package.json'));
  manifest.push({
    file: `postcss/${filename}`,
    source: `https://registry.npmjs.org/${name}/-/${name}-${version}.tgz`,
    ...(filename === 'parse.cjs' ? { build: 'tools/css-parser/build.cjs' } : {}),
    sha256: createHash('sha256').update(fs.readFileSync(path.join(destination, filename))).digest('hex'),
  });
}
fs.writeFileSync(manifestPath, JSON.stringify(manifest, null, 2) + '\n');
