// Compile-time compatibility fixtures: required null is not optional, money stays string.
import type { components } from '../../packages/api-client/src/schema';
type Input = components['schemas']['EvaluationCreate'];
const valid: Input = { market_snapshot_id: 's', settings_revision: 1, entry_price_override: null,
  volume_lots_override: null, sizing_mode: 'fixed_volume' };
// @ts-expect-error decimal wire cannot be a JavaScript number
const wrong: Input = { ...valid, entry_price_override: 1.01 };
// @ts-expect-error nullable fields must still be present
const missing: Input = { market_snapshot_id: 's', settings_revision: 1, sizing_mode: 'fixed_volume' };
type Answer = components['schemas']['AnalysisAnswer'];
type HasNoDefs = '$defs' extends keyof Answer ? false : true;
const correct: HasNoDefs = true;
void [valid, wrong, missing, correct];
