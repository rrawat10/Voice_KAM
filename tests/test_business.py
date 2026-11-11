import asyncio
from io import BytesIO
import time

from openpyxl import Workbook
import pytest

from backend.data import excel_template, mock_data, parse_excel
from backend.planner import Decision, apply_decision, validate_decision
from backend.memory import recent_turns
from backend.store import SessionStore


def workbook_bytes(rows, percent_format=False):
    wb = Workbook()
    for row in rows:
        wb.active.append(row)
    if percent_format:
        wb.active['B2'].number_format = '0%'
        wb.active['C2'].number_format = '0%'
    out = BytesIO()
    wb.save(out)
    wb.close()
    return out.getvalue()


def state():
    return {'id': 'test', 'dataset': mock_data(), 'facts': {}, 'summary': '', 'history': [], 'plan': None, 'revision': 0}


def test_actual_assignment_calculations():
    evidence = mock_data()['evidence']
    assert evidence['sales.change_percent']['value'] == -32.5
    assert evidence['orders.change_percent']['value'] == -32.5
    assert evidence['cancellation_rate.delta']['value'] == 6
    assert evidence['cancellation_rate.delta']['unit'] == 'percentage points'
    assert evidence['out_of_stock_products.delta']['value'] == 13


def test_recent_history_keeps_fifteen_complete_turns():
    history = [entry for turn in range(16) for entry in (
        {'role': 'user', 'text': f'user {turn}', 'turn': turn},
        {'role': 'assistant', 'text': f'assistant {turn}', 'turn': turn},
    )]
    kept = recent_turns(history)
    assert len(kept) == 30
    assert kept[0]['turn'] == 1
    assert kept[-1]['turn'] == 15


def test_template_roundtrip():
    result = parse_excel(excel_template(), 'example.xlsx')
    assert result['metrics'] == mock_data()['metrics']
    assert result['source'] == 'uploaded spreadsheet'


def test_missing_data_not_mock_and_zero_not_missing():
    result = parse_excel(workbook_bytes([['Metric', 'Last Month', 'This Month'], ['Sales', 0, 100]]), 'data.xlsx')
    assert result['metrics'][0]['previous'] == 0
    assert 'sales.change_percent' not in result['evidence']
    assert result['metrics'][1]['current'] is None


def test_excel_percentage_cells():
    result = parse_excel(workbook_bytes([['Metric', 'Last Month', 'This Month'], ['Cancellation rate', .08, .14]], True), 'data.xlsx')
    assert result['metrics'][2]['current'] == 14


@pytest.mark.parametrize('rows,match', [
    ([['Metric', 'Last Month', 'This Month'], ['Sales', '=1+1', 100]], 'Formulas'),
    ([['Metric', 'Last Month', 'This Month'], ['Orders', 1.5, 3]], 'whole numbers'),
    ([['Metric', 'Last Month', 'This Month'], ['Cancellation rate', .08, .14]], 'Ambiguous'),
    ([['Metric', 'Last Month', 'This Month'], ['Cancellation rate', 101, 14]], 'between'),
    ([['Metric', 'Last Month', 'This Month'], ['Sales', 1, 2], ['Sales', 2, 3]], 'Duplicate'),
    ([['Metric', 'Last Month', 'This Month'], ['Orders', -1, 4]], 'nonnegative'),
    ([['Metric', 'Last Month', 'This Month'], ['Traffic', 1, 4]], 'Unknown metric'),
    ([['x', 'y', 'z'], ['Sales', 1, 2]], 'Expected headers'),
])
def test_reject_invalid_spreadsheets(rows, match):
    with pytest.raises(ValueError, match=match):
        parse_excel(workbook_bytes(rows), 'data.xlsx')


def test_invalid_file_and_oversize():
    with pytest.raises(ValueError, match='could not be read'):
        parse_excel(b'not a workbook', 'data.xlsx')
    with pytest.raises(ValueError, match='2 MB'):
        parse_excel(b'x' * 2_000_001, 'data.xlsx')


def test_fact_requires_actual_seller_quote():
    decision = Decision(intent='explain', speech='Understood.', facts=[{'key': 'supplier', 'text': 'Supplier late', 'quote': 'supplier is late'}])
    with pytest.raises(ValueError, match='utterance'):
        validate_decision(decision, state(), 'I have no idea.')


def test_evidence_cannot_be_invented():
    decision = Decision(intent='explain', speech='Visibility declined.', findings=[{'text': 'Visibility declined.', 'status': 'observed', 'evidence_ids': ['traffic.current']}])
    with pytest.raises(ValueError, match='unavailable'):
        validate_decision(decision, state(), 'What happened?')


def test_typed_numeric_reference_must_match():
    decision = Decision(intent='explain', speech='Sales are down.', metric_references=[{'evidence_id': 'sales.change_percent', 'value': -35}])
    with pytest.raises(ValueError, match='metric value'):
        validate_decision(decision, state(), 'What happened?')


def test_correction_updates_fact_without_changing_source_data():
    current = state()
    current['facts']['revenue'] = {'text': 'Seller reports 150000', 'quote': '150000', 'turn': 1}
    decision = Decision(intent='clarify', speech='Does that cover the same period?', question='Does that cover the same period?', facts=[{'key': 'revenue', 'text': 'Seller reports 180000', 'quote': '180000'}], unknowns=['The reported revenue differs from the sheet.'])
    validate_decision(decision, current, 'Actually 180000')
    apply_decision(current, decision, 'Actually 180000', 2)
    assert current['facts']['revenue']['text'] == 'Seller reports 180000'
    assert current['dataset']['evidence']['sales.current']['value'] == 135000
    assert current['summary'] == ''
    assert not current['summary_refresh_due']
    assert 'pending_question' not in current


def test_recommendations_survive_unrelated_follow_up_and_retire_only_when_requested():
    current = state()
    current['plan'] = {'recommendations': [
        {'action': 'availability', 'title': 'Check stock', 'detail': 'Verify key items.', 'evidence_ids': ['out_of_stock_products.current']},
        {'action': 'cancellations', 'title': 'Review cancellations', 'detail': 'Check reasons.', 'evidence_ids': ['cancellation_rate.current']},
    ]}
    keep = Decision(intent='clarify', speech='What is your supplier timing?', question='What is your supplier timing?')
    apply_decision(current, keep, 'Supplier is late', 1)
    assert [item['action'] for item in current['plan']['recommendations']] == ['availability', 'cancellations']
    retire = Decision(intent='revise', speech='Let us focus on availability.', retire_recommendation_actions=['cancellations'])
    apply_decision(current, retire, 'Cancellations were already fixed.', 2)
    assert [item['action'] for item in current['plan']['recommendations']] == ['availability']


def test_isolation_expiry_and_explicit_deletion():
    async def check():
        store = SessionStore(ttl=60)
        a = state()
        await store.save(a)
        copy = await store.get('test')
        copy['facts']['invented'] = True
        assert not (await store.get('test'))['facts']
        assert await store.get('another') is None
        store.memory['test']['expires_at'] = time.time() - 1
        assert await store.get('test') is None
        await store.save(a)
        await store.delete('test')
        assert await store.get('test') is None
    asyncio.run(check())
