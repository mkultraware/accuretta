'use strict';

const fs = require('node:fs');
const parse = require('./assets/vendor/postcss/parse.cjs');

try {
  const source = fs.readFileSync(0, 'utf8');
  parse(source, { from: process.argv[2], map: false });
  process.stdout.write(JSON.stringify({ result: 'CSS syntax OK', check_status: 'passed' }));
} catch (error) {
  if (error.name !== 'CssSyntaxError') throw error;
  const line = error.input?.line || error.line;
  const column = error.input?.column || error.column;
  process.stdout.write(JSON.stringify({
    error: `CSS syntax error at line ${line}, column ${column}: ${error.reason}`,
    check_status: 'failed', line, column,
  }));
}
