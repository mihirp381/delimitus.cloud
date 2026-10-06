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
