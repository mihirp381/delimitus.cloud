// SSC-065: the cost calculator in index.html against calculator_vectors.json.
// Run with `node --test landing/calculator.test.mjs`; the Python tests check the no-script table against vector 0.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const here = new URL(".", import.meta.url);
const page = readFileSync(new URL("index.html", here), "utf8");
const { vectors } = JSON.parse(readFileSync(new URL("calculator_vectors.json", here), "utf8"));

const block = page.match(/\/\* calc:start \*\/([\s\S]*?)\/\* calc:end \*\//);
assert.ok(block, "index.html has no calc:start ... calc:end block");
const diy = new Function(`${block[1]}\nreturn diy;`)();

for (const v of vectors) {
  test(v.name, () => {
    assert.deepEqual(diy(v.in), v.out);
  });
}

const line = page.match(/\/\* line:start \*\/([\s\S]*?)\/\* line:end \*\//);
assert.ok(line, "index.html has no line:start ... line:end block");
const costLine = new Function(`${line[1]}\nreturn costLine;`)();

test("the cost line at the calculator's defaults", () => {
  const c = costLine(vectors[0].in.setup, vectors[0].in.rate);
  assert.deepEqual(
    [c.diyH, c.diyUsd, c.dlmH, c.dlmUsd, c.savedH],
    [567, 38533, 60, 4080, 507],
  );
});
