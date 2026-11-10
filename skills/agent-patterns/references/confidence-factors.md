# Confidence Factors - Detailed Scoring Rubrics

> **Scores are advisory — cite evidence, don't assert a number.** These rubrics help an agent
> *report* confidence. A high self-scored value is not evidence; a `file:line` citation, a command
> you ran, or a test that fails without the fix is. Use these factors to *communicate* uncertainty,
> not to stand in for the evidence itself. When a finding has independently been checked, weight
> that over the self-score — but the check is not something to add by default.

## Contents

- [Core Calculation Methodology](#core-calculation-methodology)
- [Domain Factors (Summary)](#domain-factors-summary)
- [Architecture Review Factors](#architecture-review-factors)
- [Specialist Domain Factors](#specialist-domain-factors)
  - [DevOps Engineer](#devops-engineer)
  - [Systems Debugger](#systems-debugger)
  - [Data Engineer](#data-engineer)
  - [Cloud Architect](#cloud-architect)
  - [Infrastructure Auditor](#infrastructure-auditor)

Factor definitions and scoring rubrics, as factor sets by role.

## Core Calculation Methodology

**Universal Formula.** A confidence score is the weighted sum of factor scores: `confidence =
sum(factor[k] * weight[k] for k in factors)`, where each factor is scored 0.0-1.0 (1.0=excellent,
0.8=good, 0.6=adequate, 0.4=poor, 0.2=critical) and weights are per-domain (see the factor sets
below). The weights for a given domain must sum to 1.0 — verify with `sum(weights.values()) == 1.0`
before using a new or edited set.

**Evidence Types**:

- Correctness: Tests passing (count/total), manual verification (steps completed), edge cases
  handled (list)
- Security: Vulnerability scans (0 critical, 2 medium), pytest exit 0 with N tests covering the
  auth path
- Performance: Benchmarks (response time 287ms avg), profiling data (the hot path's share of CPU
  time)
- Tests: Coverage percentage (87%), test quality (integration tests present), edge cases (5/5
  covered)

---

## Domain Factors (Summary)

**Backend**: correctness (0.25), security (0.25), tests (0.20), performance (0.15), patterns (0.15)

**Frontend**: correctness (0.31), types (0.25), performance (0.25), responsive (0.19)

**Architecture**: design (0.25), security (0.25), scalability (0.20), maintainability (0.20),
debt (0.10)

Backend and Frontend have only these summaries; the other roles have full sets below.

---

## Architecture Review Factors

```python
architect_factors = {
    "design_patterns": 0.0,         # Best practices, SOLID principles
    "security_architecture": 0.0,   # Defense in depth, OWASP
    "scalability_design": 0.0,      # 10x growth readiness
    "maintainability": 0.0,         # Code clarity, documentation
    "technical_debt": 0.0           # Acceptable vs problematic
}

architect_weights = {
    "design_patterns": 0.25,
    "security_architecture": 0.25,
    "scalability_design": 0.20,
    "maintainability": 0.20,
    "technical_debt": 0.10
}
```

Cloud Architect uses a dedicated factor set — see [Cloud Architect](#cloud-architect) under
Specialist Domain Factors — which supersedes this generic set for that domain.

---

## Specialist Domain Factors

### DevOps Engineer

```python
devops_factors = {
    "deployment_reliability": 0.0,
    "infrastructure_security": 0.0,
    "monitoring_coverage": 0.0,
    "operational_simplicity": 0.0,
    "cost_efficiency": 0.0
}

devops_weights = {
    "deployment_reliability": 0.25,
    "infrastructure_security": 0.25,
    "monitoring_coverage": 0.20,
    "operational_simplicity": 0.20,
    "cost_efficiency": 0.10
}
```

### Systems Debugger

```python
debug_factors = {
    "reproduction_reliability": 0.0,
    "root_cause_identified": 0.0,
    "evidence_quality": 0.0,
    "fix_clarity": 0.0,
    "edge_cases_considered": 0.0
}

debug_weights = {
    "reproduction_reliability": 0.25,
    "root_cause_identified": 0.30,  # Highest weight - finding "why" matters most
    "evidence_quality": 0.20,
    "fix_clarity": 0.15,
    "edge_cases_considered": 0.10
}
```

### Data Engineer

```python
data_engineer_factors = {
    "data_quality": 0.0,           # Validation complete at every stage?
    "pipeline_reliability": 0.0,   # Error handling, retry logic, idempotency?
    "performance": 0.0,            # Processing speed optimal for scale?
    "scalability": 0.0,            # Handles growth (millions of records)?
    "maintainability": 0.0         # Code clarity, monitoring, documentation?
}

data_engineer_weights = {
    "data_quality": 0.25,          # Data integrity paramount
    "pipeline_reliability": 0.25,  # Pipelines must be robust
    "performance": 0.20,           # Speed matters for ETL
    "scalability": 0.15,           # Growth readiness
    "maintainability": 0.15        # Team can understand/operate
}
```

**Checks**:

- Idempotency: Pipelines safe to re-run without side effects
- Dead letter queues: Failed records captured for investigation

### Cloud Architect

```python
cloud_architect_factors = {
    "infrastructure_knowledge": 0.0,   # AWS service depth, IaC fluency
    "cost_optimization": 0.0,          # Spend right-sized for company stage
    "security_design": 0.0,            # Defense in depth, least privilege
    "scalability_design": 0.0,         # Headroom without premature scaling
    "resilience_verified": 0.0         # Failure modes and recovery proven
}

cloud_architect_weights = {
    "infrastructure_knowledge": 0.20,
    "cost_optimization": 0.20,
    "security_design": 0.25,
    "scalability_design": 0.15,
    "resilience_verified": 0.20
}
```

Bespoke set — supersedes the generic Architecture Review "Cloud Architect" variation.

### Infrastructure Auditor

```python
infrastructure_auditor_factors = {
    "audit_completeness": 0.0,         # All audit areas covered?
    "finding_accuracy": 0.0,           # Findings verified, not speculative?
    "security_coverage": 0.0,          # IAM, network, secrets reviewed?
    "environment_comparison": 0.0,     # dev/uat/prod parity checked?
    "prioritization_quality": 0.0      # ROI-ranked, actionable roadmap?
}

infrastructure_auditor_weights = {
    "audit_completeness": 0.20,
    "finding_accuracy": 0.25,
    "security_coverage": 0.25,
    "environment_comparison": 0.15,
    "prioritization_quality": 0.15
}
```
