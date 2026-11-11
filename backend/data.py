"""Bounded spreadsheet input and deterministic business arithmetic."""
from io import BytesIO
import math
import zipfile

from openpyxl import Workbook, load_workbook

METRICS = {
    'sales': ('Sales', 'INR'),
    'orders': ('Orders', 'count'),
    'cancellation_rate': ('Cancellation rate', 'percent'),
    'average_selling_price': ('Average selling price', 'INR'),
    'out_of_stock_products': ('Out-of-stock products', 'count'),
}
ALIASES = {
    'sales': 'sales', 'revenue': 'sales', 'orders': 'orders',
    'cancellation rate': 'cancellation_rate', 'cancellations rate': 'cancellation_rate',
    'average selling price': 'average_selling_price', 'asp': 'average_selling_price',
    'out of stock products': 'out_of_stock_products', 'out of stock': 'out_of_stock_products',
}


def normalize(text):
    return ' '.join(str(text).strip().lower().replace('-', ' ').replace('_', ' ').split())


def dataset(values, source, filename=None):
    metrics = []
    evidence = {}
    for key, (label, unit) in METRICS.items():
        previous, current = values.get(key, (None, None))
        item = dict(id=key, label=label, unit=unit, previous=previous, current=current)
        for period, value in [('previous', previous), ('current', current)]:
            if value is not None:
                evidence[f'{key}.{period}'] = {'label': f'{label}: {period} period', 'value': value, 'unit': unit, 'source': source}
        if previous is not None and current is not None:
            delta = round(current - previous, 4)
            item['delta'] = delta
            evidence[f'{key}.delta'] = {'label': f'{label}: difference', 'value': delta, 'unit': 'percentage points' if unit == 'percent' else unit, 'source': 'calculated'}
            if previous > 0 and unit != 'percent':
                change = round(delta / previous * 100, 2)
                item['change_percent'] = change
                evidence[f'{key}.change_percent'] = {'label': f'{label}: relative change', 'value': change, 'unit': 'percent', 'source': 'calculated'}
        metrics.append(item)
    warnings = []
    if any(m['previous'] is None or m['current'] is None for m in metrics):
        warnings.append('Some metrics are missing. The assistant must treat them as unknown.')
    return dict(source=source, filename=filename, metrics=metrics, evidence=evidence, warnings=warnings)


def mock_data():
    return dataset({'sales': (200000, 135000), 'orders': (400, 270), 'cancellation_rate': (8, 14), 'average_selling_price': (500, 500), 'out_of_stock_products': (5, 18)}, 'assignment mock data')


def parse_value(cell, metric):
    value = cell.value
    if value is None or str(value).strip() == '':
        return None
    if cell.data_type == 'f':
        raise ValueError('Formulas are not supported. Paste values into the template first.')
    if isinstance(value, bool):
        raise ValueError(f'{metric}: expected a number, not true/false.')
    percent = isinstance(value, str) and value.strip().endswith('%')
    text = str(value).strip().replace('₹', '').replace(',', '').replace('%', '').strip()
    try:
        number = float(text)
    except (TypeError, ValueError):
        raise ValueError(f'{metric}: use numeric values (₹ and commas are allowed).') from None
    if not math.isfinite(number) or number < 0 or number > 1e12:
        raise ValueError(f'{metric}: values must be finite, nonnegative, and at most 1 trillion.')
    if metric == 'cancellation_rate':
        if not isinstance(value, str) and '%' in cell.number_format:
            number *= 100
        elif not percent and 0 < number < 1:
            raise ValueError('Ambiguous cancellation rate. Use 14%, 14, or an Excel percentage cell for 14 percent.')
        if number > 100:
            raise ValueError('Cancellation rate must be between 0% and 100%.')
    elif percent:
        raise ValueError(f'{metric}: percentage values are not supported for this metric.')
    if METRICS[metric][1] == 'count' and not number.is_integer():
        raise ValueError(f'{metric}: counts must be whole numbers.')
    return round(number, 4)


def parse_excel(content, filename):
    if not filename.lower().endswith('.xlsx'):
        raise ValueError('Please upload an .xlsx workbook using the downloadable template.')
    if len(content) > 2_000_000:
        raise ValueError('The upload limit is 2 MB.')
    try:
        with zipfile.ZipFile(BytesIO(content)) as z:
            if len(z.infolist()) > 100 or sum(i.file_size for i in z.infolist()) > 10_000_000:
                raise ValueError('Workbook is too large after decompression. Use the small template.')
        workbook = load_workbook(BytesIO(content), read_only=True, data_only=False, keep_links=False)
    except ValueError:
        raise
    except Exception:
        raise ValueError('This file could not be read as an Excel workbook.') from None
    try:
        sheet = workbook.worksheets[0]
        if sheet.max_row > 100 or sheet.max_column > 3:
            raise ValueError('Use one table with 3 columns and at most 100 rows.')
        rows = list(sheet.iter_rows())
        if not rows or [normalize(c.value) for c in rows[0]] != ['metric', 'last month', 'this month']:
            raise ValueError('Expected headers: Metric | Last Month | This Month. Download the template below.')
        values = {}
        for row in rows[1:]:
            if all(c.value is None for c in row):
                continue
            key = ALIASES.get(normalize(row[0].value))
            if not key:
                raise ValueError('Unknown metric. Supported: Sales, Orders, Cancellation rate, Average selling price, Out-of-stock products.')
            if key in values:
                raise ValueError(f'Duplicate metric: {METRICS[key][0]}.')
            values[key] = (parse_value(row[1], key), parse_value(row[2], key))
        if not any(a is not None and b is not None for a, b in values.values()):
            raise ValueError('Provide both period values for at least one metric.')
        result = dataset(values, 'uploaded spreadsheet', filename[:120])
        if len(workbook.worksheets) > 1:
            result['warnings'].append('Only the first worksheet was imported.')
        return result
    finally:
        workbook.close()


def excel_template():
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = 'Seller metrics'
    sheet.append(['Metric', 'Last Month', 'This Month'])
    for metric in mock_data()['metrics']:
        sheet.append([metric['label'], metric['previous'], metric['current']])
    for col in ['A', 'B', 'C']:
        sheet.column_dimensions[col].width = 27
    sheet.freeze_panes = 'B2'
    stream = BytesIO()
    workbook.save(stream)
    workbook.close()
    return stream.getvalue()
