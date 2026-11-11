"""Calculations requested by a model, executed only on referenced numeric evidence."""
import math
from typing import Literal

from pydantic import BaseModel, Field


class CalculationRequest(BaseModel):
    id: str = Field(pattern=r'^[a-z][a-z0-9_]{0,39}$')
    operation: Literal['difference', 'percent_change', 'ratio']
    baseline_ref: str
    current_ref: str
    label: str = Field(max_length=100)


def numeric_evidence(state, proposed_facts=()):
    evidence = dict(state['dataset']['evidence'])
    # Derived calculation results live in the same evidence collection as the
    # spreadsheet numbers. Re-expose one only if its original inputs still agree.
    calculated = {key: item for key, item in evidence.items()
                  if item.get('source') == 'Python calculation'}
    for key in calculated:
        evidence.pop(key)
    facts = dict(state['facts'])
    facts.update({fact.key: fact.model_dump() for fact in proposed_facts})
    for key, fact in facts.items():
        if fact.get('numeric_value') is not None:
            evidence['seller.' + key] = {
                'value': fact['numeric_value'], 'unit': fact.get('unit', 'count'),
                'label': fact['text'], 'source': 'seller ' + fact.get('kind', 'reported'),
            }
    # Reuse results only while their input values and provenance still match.
    for key, item in calculated.items():
        if all(ref in evidence and evidence[ref]['value'] == value
               for ref, value in zip(item['input_refs'], item['input_values'])):
            evidence[key] = item
    return evidence


def calculate(request, evidence):
    if request.baseline_ref not in evidence or request.current_ref not in evidence:
        raise ValueError('Calculation needs known numeric source references.')
    left, right = evidence[request.baseline_ref], evidence[request.current_ref]
    a, b = left['value'], right['value']
    if not all(isinstance(n, (int, float)) and math.isfinite(n) for n in [a, b]):
        raise ValueError('Calculation inputs must be finite numbers.')
    if request.operation != 'ratio' and left['unit'] != right['unit']:
        raise ValueError('These quantities have different units and cannot be compared directly.')
    if request.operation == 'difference':
        value = b - a
        unit = 'percentage points' if left['unit'] == 'percent' else left['unit']
    elif request.operation == 'percent_change':
        if a <= 0:
            raise ValueError('A percentage change needs a positive baseline.')
        value, unit = (b - a) / a * 100, 'percent'
    else:
        if a == 0:
            raise ValueError('Cannot divide by zero.')
        value, unit = b / a, f"{right['unit']} / {left['unit']}"
    if not math.isfinite(value) or abs(value) > 1e15:
        raise ValueError('Calculation result is outside the supported range.')
    return {
        'label': request.label, 'value': round(value, 4), 'unit': unit,
        'source': 'Python calculation', 'operation': request.operation,
        'input_refs': [request.baseline_ref, request.current_ref], 'input_values': [a, b],
        'qualification': 'Uses a seller-stated or hypothetical input; not a verified spreadsheet correction.' if any(str(x['source']).startswith('seller') or x.get('qualification') for x in [left, right]) else 'Arithmetic comparison; does not establish causation or period comparability.',
    }
