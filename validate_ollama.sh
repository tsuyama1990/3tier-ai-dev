#!/bin/bash
# Ollama Validation Master Script: Step 1 → Step 4 Sequential Execution

set -euo pipefail

echo "=== [PRE-CHECK] Ollama Connectivity Check ==="
curl -sf http://localhost:11434/api/tags > /dev/null || {
    echo "ERROR: Ollama is not running. Execute 'ollama serve' first."
    exit 1
}
echo "Ollama: OK"

echo ""
echo "=== [STEP 1] Baseline Communication Test ==="
.venv/bin/python -m pytest tests/step1_baseline/test_ollama_baseline.py -v --timeout=700 2>&1 | tee step1_result.log
STEP1_EXIT=${PIPESTATUS[0]}

echo ""
echo "=== [STEP 2] Fake API Reference Test ==="
.venv/bin/python -m pytest tests/step2_fake_api/test_fake_api_ref.py -v --timeout=700 2>&1 | tee step2_result.log
STEP2_EXIT=${PIPESTATUS[0]}

echo ""
echo "=== [STEP 3] Self-Healing Stress Test ==="
.venv/bin/python -m pytest tests/step3_stress/test_self_healing_stress.py -v \
    --timeout=2000 2>&1 | tee step3_result.log
STEP3_EXIT=${PIPESTATUS[0]}

echo ""
echo "=== [STEP 4] Asset Synthesizer Ollama Test ==="
.venv/bin/python -m pytest tests/step4_ollama_synthesizer/test_ollama_synthesizer.py -v \
    --timeout=700 2>&1 | tee step4_result.log
STEP4_EXIT=${PIPESTATUS[0]}

echo ""
echo "=== [SUMMARY] ==="
[ $STEP1_EXIT -eq 0 ] && echo "Step 1: PASS ✅" || echo "Step 1: FAIL ❌"
[ $STEP2_EXIT -eq 0 ] && echo "Step 2: PASS ✅" || echo "Step 2: FAIL ❌"
[ $STEP3_EXIT -eq 0 ] && echo "Step 3: PASS ✅" || echo "Step 3: FAIL ❌"
[ $STEP4_EXIT -eq 0 ] && echo "Step 4: PASS ✅" || echo "Step 4: FAIL ❌"

exit $(( STEP1_EXIT + STEP2_EXIT + STEP3_EXIT + STEP4_EXIT ))
