// @ts-check
/**
 * Enterprise blueprint: playwright-js-v1 — oracle data loader.
 * Expected values live in testdata/*.json so oracle data stays separated
 * from test logic (parity with the legacy TestDataLoader).
 */
import fs from 'node:fs';
import path from 'node:path';

const DATA_FILE = path.resolve('testdata/expected-drugs.json');

let cached = null;

export function loadExpectedDrugData() {
  if (!cached) {
    cached = JSON.parse(fs.readFileSync(DATA_FILE, 'utf-8'));
  }
  return cached;
}

/**
 * @param {string} key e.g. 'ATORVASTATIN_GENERIC'
 */
export function findDrugByKey(key) {
  const drug = loadExpectedDrugData().drugs.find((d) => d.key === key);
  if (!drug) {
    throw new Error(`No expected drug data for key: ${key}`);
  }
  return drug;
}
