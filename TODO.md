# TODO - Fix word-swapped/float behavior and verify port

## Plan
1. Implement `word_swapped` handling for Modbus float32/float64 (16-bit word swap) in `PymodbusService`:
   - Add helper that optionally swaps the two 16-bit registers for float32 and four 16-bit registers for float64.
   - Apply swap during both decode and encode.
   - Ensure it is driven by `payload['plc']['word_swapped']` (bridge) and by PLC read form (`word_swapped`) when applicable.

2. Implement passing `word_swapped` into Modbus read/write calls from bridge sync:
   - In `AsyncBridgeService._sync_payload`, read `payload['plc']['word_swapped']` and pass it to `PymodbusService` decode/encode.

3. Implement Allen-Bradley (Logix) word swap behavior if pycomm3 supports it (or adjust tag name/packing appropriately). If not supported, document limitation.

4. Rebuild portable EXE (optional) and smoke test:
   - Test Modbus float32/float64 with `word_swapped=yes` against known PLC values.
   - Verify port used in bridge sync by ensuring `last_plc_port` is correctly stored into payload.

## Done tracking
- [ ] Step 1: Modbus float word-swapped decode/encode
- [ ] Step 2: Bridge wiring of word_swapped into Modbus conversions
- [ ] Step 3: Logix word-swapped behavior (if applicable)
- [ ] Step 4: Port verification


## Progress
- Added this TODO to track implementation work.


