import test from 'node:test';
import assert from 'node:assert/strict';
import { ratingTier } from '../src/components/ratingScale.ts';

test('rating colors keep the original 9, 9.5 and 9.9 boundaries', () => {
  for (const [score, expected] of [[null,'unrated'],[0,'black'],[8.9,'black'],[9,'orange-red'],[9.4,'orange-red'],[9.5,'red'],[9.8,'red'],[9.9,'rainbow'],[10,'rainbow']]) {
    assert.equal(ratingTier(score), expected, String(score));
  }
});
