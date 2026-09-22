# Carleman Weight Function Construction (Mar 23, 2026)

This problem is an open research problem contributed by **Qiao Zhuang** (University of Missouri-Kansas City), **Qihao Ye** (University of California, San Diego), **Zhongqiang Zhang** (Worcester Polytechnic Institute), and **Yu Wang** (Southwest Jiaotong University).

## Results

An early version of QED found the logarithmic candidate `ψ(x,t) = −log(1 + x + ct) − log(1 + ct)` with sample parameters. The authors independently derived its admissible parameter ranges and proved the Carleman estimate and subsequent stability and reconstruction results. See [the paper, Sections 3–6 and Appendices A–B](https://arxiv.org/html/2609.06283v1).

The archived run mainly used Claude Opus 4.6, as documented in the [search archive](https://github.com/proofQED/QED_wave_Carleman). Current QED model defaults do not describe that historical run.

## Workflow

- Authors supplied analytical screening criteria.
- Seven rounds of candidate search used symbolic and numerical checks and failure feedback.
- Authors restored the full mixed-term condition and established the quantitative bounds needed for the proof.

## Expert Comments from Qiao Zhuang, Qihao Ye, Zhongqiang Zhang, and Yu Wang

The use of LLM-based search aims to replace the traditional hand-crafted process of identifying suitable Carleman weight functions for deriving Carleman estimates. This process is often difficult, highly problem-dependent, and time-consuming, and may still fail to produce a valid construction through manual trial. By automating the search over admissible weight functions under the required structural and inequality constraints, the approach has the potential to systematically explore viable candidates, thereby reducing reliance on ad hoc intuition and extensive analytical effort. This is particularly important for inverse problems, where the construction of an appropriate Carleman weight is a central and highly nontrivial step that directly impacts stability and uniqueness results. An LLM-assisted search framework can help identify feasible weight structures, guide the design of admissible functions, and accelerate the development of Carleman-based methodologies, thereby extending their applicability to more complex or less structured problems.

## Search Records and Verification Report

The [public search archive](https://github.com/proofQED/QED_wave_Carleman) contains the problem, prompts, candidates, failed attempts, verification reports, and execution logs. The final [Verification Report — Round 7](https://github.com/proofQED/QED_wave_Carleman/blob/893b18ae7f0a701f40883d4df318c1354a70b903/candidates/winning_report.md) records the AI-generated checks for the simplified search problem, including sample parameters `α = 1`, `s = −1/100`, and `λ = −1/20`. Cite it as a discovery record; use the paper for the final proofs.

## Citations

Chenyang An, Yu Wang, Qihao Ye, Zhongqiang Zhang, and Qiao Zhuang (2026). [Carleman Estimates for Wave Equations on the Half-Line: AI-Assisted Weights and Applications](https://arxiv.org/abs/2609.06283). arXiv:2609.06283 [math.AP].

```bibtex
@article{an2026carleman,
    title={Carleman Estimates for Wave Equations on the Half-Line: {AI}-Assisted Weights and Applications},
    author={An, Chenyang and Wang, Yu and Ye, Qihao and Zhang, Zhongqiang and Zhuang, Qiao},
    journal={arXiv preprint arXiv:2609.06283},
    year={2026}
}
```

For the QED system itself, cite the [system report and its BibTeX entry](../../README.md).
