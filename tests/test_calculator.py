import asyncio
import json
from types import SimpleNamespace as NS
import pytest

from backend.calculator import CalculationRequest, calculate, numeric_evidence
from backend.config import Settings
from backend.data import mock_data
from backend.planner import Decision, plan_turn, apply_decision


def test_updated_seller_figure_calculates_conditionally():
    evidence = mock_data()['evidence']
    evidence['seller.revenue'] = {'value': 180000, 'unit': 'INR', 'source': 'seller reported'}
    result = calculate(CalculationRequest(id='updated_decline', operation='percent_change', baseline_ref='sales.previous', current_ref='seller.revenue', label='Change using seller figure'), evidence)
    assert result['value'] == -10
    assert 'seller-stated' in result['qualification']


def test_no_arbitrary_values_unit_mismatch_or_division_by_zero():
    evidence = mock_data()['evidence']
    for baseline, current, match in [('fake', 'sales.current', 'known'), ('orders.previous', 'sales.current', 'units')]:
        with pytest.raises(ValueError, match=match):
            calculate(CalculationRequest(id='x', operation='difference', baseline_ref=baseline, current_ref=current, label='x'), evidence)
    evidence['sales.previous']['value'] = 0
    with pytest.raises(ValueError, match='positive baseline'):
        calculate(CalculationRequest(id='x', operation='percent_change', baseline_ref='sales.previous', current_ref='sales.current', label='x'), evidence)


def test_two_pass_calculation_and_invalidation_on_correction():
    async def check():
        state = {'id': 'test', 'dataset': mock_data(), 'facts': {}, 'history': [], 'summary': '', 'plan': None, 'revision': 0}
        responses = [
            Decision(intent='explain', speech='', facts=[{'key': 'revenue', 'text': 'Seller reports 180000', 'quote': '180000', 'numeric_value': 180000, 'unit': 'INR'}], calculation_requests=[{'id': 'updated', 'operation': 'percent_change', 'baseline_ref': 'sales.previous', 'current_ref': 'seller.revenue', 'label': 'Change using reported revenue'}]),
            Decision(intent='explain', speech='Using your figure, sales would be down 10 percent.', evidence_ids=['calculated.updated'], metric_references=[{'evidence_id': 'calculated.updated', 'value': -10}]),
        ]
        calls = []
        async def generate_content(**kwargs):
            calls.append(json.loads(kwargs['contents']))
            return NS(text=responses.pop(0).model_dump_json())
        client = NS(aio=NS(models=NS(generate_content=generate_content)))
        plan = await plan_turn(client, Settings(), state, 'Actually 180000')
        assert len(calls) == 2
        assert calls[1]['EVIDENCE']['calculated.updated']['value'] == -10
        apply_decision(state, plan, 'Actually 180000', 1)
        assert state['dataset']['evidence']['calculated.updated']['value'] == -10
        assert state['summary'] == ''
        state['facts']['revenue']['numeric_value'] = 190000
        assert 'calculated.updated' not in numeric_evidence(state)
    asyncio.run(check())
