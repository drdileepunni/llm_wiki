---
title: ARC-AGI Challenge
type: entity
tags: [benchmark, artificial-general-intelligence, reasoning]
created: 2026-04-05
updated: 2026-04-05
sources: [wang-hrm-2025]
---

# ARC-AGI Challenge

The Abstraction and Reasoning Corpus for Artificial General Intelligence (ARC-AGI) is a benchmark designed to evaluate general fluid intelligence through IQ-test-like puzzles requiring inductive reasoning. Originally introduced by François Chollet, it is considered a key test for measuring progress toward artificial general intelligence.

## Task Structure

ARC-AGI presents challenges as input-output grid pairs:
- Each task provides 2-3 input-output example pairs
- AI system must extract and generalize abstract rules from examples
- Must produce correct output grid for test input
- Two attempts allowed per test input
- Grids typically range from small (3×3) to medium (30×30) sizes

## Versions

### ARC-AGI-1 (Original)
- Initial benchmark focusing on core inductive reasoning
- ~400 training tasks, ~400 evaluation tasks
- Exposes fundamental limitations in current AI approaches
- Human performance: ~80%
- Best AI performance before HRM: ~34.5% (o3-mini-high)

### ARC-AGI-2 (Expanded)
- More comprehensive and refined task collection
- 1120 training examples total
- Emphasizes deeper compositional reasoning
- Requires multi-step logic and contextual rule application
- More challenging for AI systems while remaining human-solvable
- Human calibration shows tasks are challenging but achievable

## Why It's Hard for AI

From [[wang-hrm-2025]]:

1. **Few-shot generalization**: Must learn from only 2-3 examples
2. **Novel tasks**: Each evaluation task is entirely new, not seen during training
3. **Abstract reasoning**: Requires extracting high-level patterns and rules
4. **Compositional structure**: Solutions often involve combining multiple transformations
5. **No linguistic scaffolding**: Pure visual-spatial reasoning without text

## Performance Benchmarks

From [[wang-hrm-2025]] on ARC-AGI-1 evaluation set:

**Large Language Models (with CoT):**
- o3-mini-high: 34.5%
- Claude 3.7 8K: 21.2%
- Deepseek R1: 21.0%
- Direct prediction baseline: 15.8%

**HRM (27M parameters, 1000 examples, no pre-training/CoT):**
- ARC-AGI-1: 40.3%
- ARC-AGI-2: 5.0%

Notably, HRM achieves this with:
- ~27M parameters vs billions in LLMs
- 900 token context (30×30 grid) vs 8K+ tokens
- Trained from scratch vs pre-trained
- Direct prediction vs Chain-of-Thought

## Task Characteristics

Common patterns in ARC tasks:
- Spatial transformations (rotation, reflection, translation)
- Color-based rules and substitutions
- Object counting and grouping
- Pattern completion and extrapolation
- Logical operations (AND, OR, XOR on spatial patterns)
- Symmetry detection and generation

## Significance for AGI

Some researchers believe mastering ARC-AGI would signal true artificial general intelligence. However, the primary purpose is to:
- Expose current roadblocks in AGI progress
- Measure core cognitive abilities independent of knowledge
- Test abstraction and reasoning rather than pattern matching
- Evaluate few-shot learning and generalization

## HRM's Approach

From [[wang-hrm-2025]], HRM solves ARC tasks through:

1. **Data augmentation**: Translations, rotations, flips, color permutations
2. **Task embeddings**: Learnable special tokens for each puzzle
3. **Test-time augmentation**: Generate and solve 1000 augmented variants
4. **Voting**: Select two most popular predictions as final outputs
5. **Iterative refinement**: Makes incremental adjustments similar to hill-climbing

Visualization of intermediate steps shows HRM:
- Starts with initial board state
- Makes progressive modifications
- Iteratively improves solution
- Follows more consistent progression than backtracking-heavy approaches

## Related Benchmarks

- [[Sudoku]]: Tests logical constraint satisfaction
- [[Maze navigation]]: Tests pathfinding and spatial reasoning
- Both require different reasoning strategies than ARC's inductive pattern matching