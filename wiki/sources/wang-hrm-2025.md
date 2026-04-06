---
title: Hierarchical Reasoning Model
type: source
tags: [neural-architecture, reasoning, deep-learning, ARC-AGI, sudoku, cognitive-neuroscience]
created: 2026-04-05
updated: 2026-04-05
sources: []
---

# Hierarchical Reasoning Model

**Citation:** Wang G, Li J, Sun Y, Chen X, Liu C, Wu Y, Lu M, Song S, Abbasi Yadkori Y. Hierarchical Reasoning Model. arXiv:2506.21734v2 [cs.AI]. 2025.

## Abstract

This paper introduces the Hierarchical Reasoning Model (HRM), a novel recurrent neural architecture inspired by hierarchical and multi-timescale processing in the human brain. HRM addresses fundamental limitations of current large language models in reasoning tasks by achieving significant computational depth while maintaining training stability and efficiency. With only 27 million parameters, HRM achieves exceptional performance on complex reasoning benchmarks using minimal training data (≤1000 examples), operating without pre-training or chain-of-thought supervision.

## Key Findings

- **Performance on reasoning benchmarks**: HRM achieves 40.3% on ARC-AGI-1 (vs 34.5% for o3-mini-high), near-perfect accuracy (55%) on Sudoku-Extreme, and 74.5% on Maze-Hard tasks where CoT-based models score 0%
- **Minimal data requirements**: Trained with only ~1000 examples per task, without pre-training or CoT data
- **Hierarchical convergence**: Two-module architecture (high-level for abstract planning, low-level for detailed computation) enables deep sequential reasoning through nested convergence cycles
- **Efficient training**: One-step gradient approximation eliminates backpropagation through time (BPTT), reducing memory from O(T) to O(1)
- **Biological correspondence**: Emergent dimensionality hierarchy (PR ratio ~2.98) matches mouse cortical organization (~2.25)
- **Adaptive computation**: ACT mechanism dynamically allocates computational resources based on task complexity
- **Inference-time scaling**: Performance improves with additional computation during inference

## Entities Mentioned

- [[Hierarchical Reasoning Model]]
- [[Chain-of-Thought]]
- [[ARC-AGI Challenge]]
- [[Sudoku]]
- [[Transformer Architecture]]
- [[Recurrent Neural Networks]]
- [[Adaptive Computation Time]]
- [[Deep Equilibrium Models]]

## Concepts Mentioned

- [[Computational Depth]]
- [[Hierarchical Convergence]]
- [[Latent Reasoning]]
- [[One-Step Gradient Approximation]]
- [[Deep Supervision]]
- [[Participation Ratio]]
- [[Dimensionality Hierarchy]]
- [[Inference-Time Scaling]]

## Open Questions

1. What specific algorithmic strategies does HRM learn for different reasoning tasks (DFS for Sudoku vs hill-climbing for ARC)?
2. Can the hierarchical architecture be scaled to even larger models while preserving efficiency?
3. How does HRM's performance compare on tasks requiring different forms of reasoning (symbolic vs spatial vs causal)?
4. Is the emergent dimensionality hierarchy causally necessary for performance, or merely correlational?
5. Can similar principles be applied to other modalities beyond grid-based reasoning tasks?