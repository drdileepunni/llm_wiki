---
title: Hierarchical Reasoning Model
type: entity
tags: [neural-architecture, deep-learning, reasoning]
created: 2026-04-05
updated: 2026-04-05
sources: [wang-hrm-2025]
---

# Hierarchical Reasoning Model (HRM)

A novel recurrent neural architecture designed for complex reasoning tasks, introduced by Wang et al. (2025). HRM achieves significant [[Computational Depth]] while maintaining training stability and efficiency through brain-inspired hierarchical processing.

## Architecture

HRM consists of four learnable components:

1. **Input network** (fI): Converts discrete tokens to vector representations
2. **Low-level module** (fL): Fast, detailed computations operating at short timescales
3. **High-level module** (fH): Slow, abstract planning operating at longer timescales
4. **Output network** (fO): Transforms hidden states to output predictions

The model executes N high-level cycles of T low-level timesteps each, totaling N×T computational steps per forward pass.

## Key Mechanisms

### Hierarchical Convergence

Unlike standard recurrent networks that converge too quickly and lose computational power, HRM implements "hierarchical convergence":

- The L-module converges to a local equilibrium within each T-step cycle
- The H-module updates once per cycle, establishing new context for L-module
- L-module is effectively "reset" each cycle, beginning new convergence phase
- This enables effective depth of NT steps while maintaining stable convergence

### One-Step Gradient Approximation

HRM uses an efficient training method that:
- Eliminates backpropagation through time (BPTT)
- Reduces memory footprint from O(T) to O(1)
- Based on Deep Equilibrium Models and Implicit Function Theorem
- More biologically plausible than BPTT
- Gradient path: Output → final H-state → final L-state → input

### Adaptive Computation Time (ACT)

HRM incorporates a Q-learning mechanism to dynamically determine computation time:
- Adapts number of forward passes based on task complexity
- Enables "thinking fast and slow" like human cognition
- Achieves computational savings with minimal performance impact
- Supports inference-time scaling by increasing Mmax parameter

## Performance

From [[wang-hrm-2025]]:

**With ~1000 training examples, no pre-training, no CoT:**
- ARC-AGI-1: 40.3% (vs 34.5% for o3-mini-high)
- ARC-AGI-2: 5.0% (vs 3.0% for o3-mini-high)
- Sudoku-Extreme: 55% (vs 0% for all CoT models)
- Maze-Hard (30×30): 74.5% (vs 0% for all CoT models)

**Key advantages over baselines:**
- Only 27M parameters vs billions in LLMs
- Context length of 900 tokens vs 8K+ in competing models
- Trained from scratch vs pre-trained models
- Direct prediction vs Chain-of-Thought reasoning

## Biological Correspondence

HRM exhibits emergent properties matching neuroscience findings:

- **Dimensionality hierarchy**: High-level module operates in higher-dimensional space (PR=89.95) than low-level module (PR=30.22)
- **Ratio match**: zH/zL ratio of ~2.98 closely matches mouse cortex hierarchy (~2.25)
- **Scaling behavior**: High-level dimensionality scales with task diversity, low-level remains stable
- These properties emerge through training, not architectural design

## Implementation Details

- Both modules use Transformer encoder blocks
- Post-Norm architecture with RMSNorm
- Rotary positional encoding, gated linear units
- Optimized with Adam-atan2 (scale-invariant variant)
- States initialized from truncated normal distribution
- Stablemax activation for small-sample scenarios

## Related Work

HRM builds on and differs from:
- [[Neural Turing Machines]] and [[Differentiable Neural Computer]]: Similar iterative computation, but HRM uses hierarchical structure
- [[Universal Transformers]]: Also adds recurrence to Transformers, but lacks hierarchical separation
- [[Recurrent Relational Networks]]: Graph-based algorithm learning vs HRM's latent reasoning
- [[Chain-of-Thought]]: HRM performs latent reasoning vs explicit linguistic decomposition

## Limitations and Future Directions

1. Performance gains from extra compute are task-dependent (strong for Sudoku, minimal for ARC)
2. Biological correspondence is correlational; causal necessity unclear
3. Limited to sequence-to-sequence tasks on grid-based problems so far
4. Further work needed to understand learned algorithmic strategies