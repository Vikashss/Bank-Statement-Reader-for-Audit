import re
from io import BytesIO

import pandas as pd
import pdfplumber
import streamlit as st

st.set_page_config(page_title="Bank Statement Parser", page_icon="📄", layout="wide")

DATE_START_RE = re.compile(r'^\s*(\d{2}-\d{2}-\d{4})\b')
DATE_ANY_RE = re.compile(r'(\d{2}-\d{2}-\d{4})')
BAL_RE = re.compile(r'(\d+(?:,\d{3})*\.\d{2}(?:Cr|Dr))')
REF_CODE_RE = re.compile(r'\b[A-Z]{4}\d{6,}\b')

SKIP_TEXT = [
    "JAMMU AND KASHMIR BANK LTD",
    "MOVING SECRETARIAT",
    "MOVING SECRETRAIT",
    "CIVIL SECRETARIAT",
    "CIVIL SECRETRAIT",
    "IFSC Code",
    "MICR Code",
    "PHONE Code",
    "TYPE:",
    "A/C NO:",
    "Printed By",
    "STATEMENT OF ACCOUNT",
    "Transaction Details Page",
    "Date Stamp Manager",
    "Unless the constituent",
    "immediately of any discrepancy found",
    "by him in this statement of Account",
    "it will be taken that he has found",
    "the account correct",
    "Interest Rate",
    "No Nomination",
    "No Nomination Available",
    "cKYC Id",
    "TO:",
    "OPP GURUDWARA",
    "CHANNI RAMA JAMMU",
    "JAMMU,JAMMU AND KASHMIR",
    "180001",
    "https://",
    "http://",
    "Grand Total:",
    "Funds in clearing:",
    "Total available Amount:",
    "Effective Available Amount:",
    "Effective Available Amount",
    "FFD Contribution:",
    "FFD Contribution",
    "Page Total:",
    "Printed By ****END OF STATEMENT****",
    "END OF STATEMENT",
]

STOP_WORDS = [
    "Grand Total:",
    "Funds in clearing:",
    "Total available Amount:",
    "Effective Available Amount:",
    "Effective Available Amount",
    "FFD Contribution:",
    "FFD Contribution",
    "Page Total:",
    "Printed By ****END OF STATEMENT****",
    "END OF STATEMENT",
]

DISPLAY_COLUMNS = [
    "Date",
    "Description",
    "IFSC / Ref No",
    "Parsed Amount",
    "Debit",
    "Credit",
    "Closing Balance",
    "Correction Flag",
    "Correction Note",
]


def clean(text):
    return " ".join(str(text).split()) if text is not None else ""


def should_skip(line):
    line = clean(line)
    if not line:
        return True
    return any(x in line for x in SKIP_TEXT)


def balance_to_float(balance_text):
    balance_text = clean(balance_text)
    if not balance_text:
        return None

    sign = -1 if balance_text.endswith("Dr") else 1
    num = balance_text.replace("Cr", "").replace("Dr", "").replace(",", "").strip()

    try:
        return sign * float(num)
    except Exception:
        return None


def amount_to_float(amount_text):
    amount_text = clean(amount_text).replace(",", "")
    try:
        return float(amount_text)
    except Exception:
        return None


def fmt_amount(x):
    if x is None:
        return "0"
    return f"{float(x):.2f}"


def split_description_and_ref(text):
    text = clean(text)
    if not text:
        return "", ""

    ref_match = REF_CODE_RE.search(text)
    ref_code = ref_match.group(0) if ref_match else ""

    if ref_code:
        desc = re.sub(r'\b' + re.escape(ref_code) + r'\b', '', text).strip()
        desc = clean(desc)
        return desc, ref_code

    return text, ""


def cut_footer_text(block):
    block = clean(block)
    for word in STOP_WORDS:
        pos = block.find(word)
        if pos != -1:
            block = block[:pos].strip()
    return block


def parse_transaction_block(block):
    block = cut_footer_text(block)
    if not block:
        return None

    m_date = DATE_ANY_RE.search(block)
    if not m_date:
        return None
    date = m_date.group(1)

    balances = BAL_RE.findall(block)
    if not balances:
        return None
    closing_balance = balances[-1]

    bal_pos = block.rfind(closing_balance)
    usable = block[:bal_pos + len(closing_balance)].strip()
    usable = usable.replace(date, "", 1).strip()

    pattern = (
        r'^(.*)\s'
        r'(\d+(?:,\d{3})*\.\d{2})\s'
        r'(' + re.escape(closing_balance) + r')$'
    )

    m = re.search(pattern, usable)
    if not m:
        return None

    left_text = m.group(1).strip()
    txn_amount = m.group(2).strip()
    description, ref_code = split_description_and_ref(left_text)

    return {
        "date": date,
        "description": description,
        "ref_code": ref_code,
        "amount": txn_amount,
        "closing_balance": closing_balance,
    }


def build_transaction_blocks(file_obj):
    blocks = []
    current_block = ""

    file_obj.seek(0)
    with pdfplumber.open(file_obj) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if not text:
                continue

            for raw_line in text.split("\n"):
                line = clean(raw_line)
                if not line:
                    continue

                if should_skip(line):
                    continue

                if DATE_START_RE.match(line):
                    if current_block:
                        blocks.append(current_block.strip())
                    current_block = line
                else:
                    if current_block:
                        current_block += " " + line

    if current_block:
        blocks.append(current_block.strip())

    return blocks


def process_pdf(file_obj, opening_balance=None):
    blocks = build_transaction_blocks(file_obj)

    parsed_rows = []
    failed_blocks = []

    for block in blocks:
        row = parse_transaction_block(block)
        if row:
            parsed_rows.append(row)
        else:
            failed_blocks.append(block)

    final_rows = []
    prev_balance = opening_balance

    for row in parsed_rows:
        date = row["date"]
        description = row["description"]
        ref_code = row["ref_code"]
        parsed_amount = row["amount"]
        closing_balance = row["closing_balance"]

        curr_balance = balance_to_float(closing_balance)
        parsed_amt_float = amount_to_float(parsed_amount)

        debit = "0"
        credit = "0"
        final_amount = parsed_amt_float
        correction_flag = "No"
        correction_note = ""

        text_check = (description + " " + ref_code).upper()

        if prev_balance is None or curr_balance is None:
            final_amount = parsed_amt_float
            debit = fmt_amount(final_amount) if final_amount is not None else "0"
            credit = "0"
            correction_note = "Opening balance / previous balance unavailable"
        else:
            delta = round(curr_balance - prev_balance, 2)
            abs_delta = round(abs(delta), 2)

            if parsed_amt_float is None or round(parsed_amt_float, 2) != abs_delta:
                final_amount = abs_delta
                correction_flag = "Yes"
                correction_note = f"Parsed amount replaced by balance difference {abs_delta:.2f}"
            else:
                final_amount = parsed_amt_float

            if delta < 0:
                debit = fmt_amount(final_amount)
                credit = "0"
            elif delta > 0:
                debit = "0"
                credit = fmt_amount(final_amount)
            else:
                reversal_words = [
                    "REV", "REVERSED", "RETURN", "RETURNED",
                    "INVALID", "FROM:", "B/F", "ACC CLOSED",
                ]
                if any(word in text_check for word in reversal_words):
                    debit = "0"
                    credit = fmt_amount(final_amount) if final_amount is not None else "0"
                    correction_note = correction_note or "Same balance row classified as credit by keyword"
                else:
                    debit = fmt_amount(final_amount) if final_amount is not None else "0"
                    credit = "0"
                    correction_note = correction_note or "Same balance row classified as debit by default"

        final_rows.append([
            date,
            description,
            ref_code,
            parsed_amount,
            debit,
            credit,
            closing_balance,
            correction_flag,
            correction_note,
        ])

        prev_balance = balance_to_float(closing_balance)

    df = pd.DataFrame(final_rows, columns=DISPLAY_COLUMNS)

    if not df.empty:
        df["Debit_num"] = pd.to_numeric(df["Debit"], errors="coerce").fillna(0.0)
        df["Credit_num"] = pd.to_numeric(df["Credit"], errors="coerce").fillna(0.0)

    return df, failed_blocks, len(blocks)


def to_excel_bytes(df):
    output = BytesIO()
    export_df = df.drop(columns=["Debit_num", "Credit_num"], errors="ignore")

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        export_df.to_excel(writer, index=False, sheet_name="Statement")
        ws = writer.sheets["Statement"]

        for idx, column_name in enumerate(export_df.columns, start=1):
            max_len = max(
                len(str(column_name)),
                *(len(str(v)) for v in export_df[column_name].fillna(""))
            )
            col_letter = ws.cell(row=1, column=idx).column_letter
            ws.column_dimensions[col_letter].width = min(max(max_len + 2, 12), 50)

    output.seek(0)
    return output


st.title("📄 Bank Statement Parser")
st.caption("Upload statement PDF, review parsed rows, and download Excel.")

uploaded_file = st.file_uploader("Upload statement PDF", type=["pdf"])

opening_balance_input = st.text_input(
    "Enter Opening Balance manually (example: 90817476.00Cr or 1250.00Dr)",
    value=""
)

opening_balance = balance_to_float(opening_balance_input) if opening_balance_input.strip() else None

if opening_balance_input.strip() and opening_balance is None:
    st.error("Invalid opening balance format. Use format like 90817476.00Cr or 1250.00Dr")
    st.stop()

if uploaded_file is None:
    st.info("Upload a PDF to start.")
else:
    try:
        with st.spinner("Processing PDF..."):
            df, failed_blocks, total_blocks = process_pdf(uploaded_file, opening_balance=opening_balance)

        if df.empty:
            st.error("No transactions could be parsed from the uploaded PDF.")
        else:
            total_rows = len(df)
            total_debit = float(df["Debit_num"].sum())
            total_credit = float(df["Credit_num"].sum())
            corrected_rows = int((df["Correction Flag"] == "Yes").sum())
            failed_count = len(failed_blocks)

            m1, m2, m3, m4, m5 = st.columns(5)
            m1.metric("Rows", total_rows)
            m2.metric("Total Debit", f"{total_debit:,.2f}")
            m3.metric("Total Credit", f"{total_credit:,.2f}")
            m4.metric("Corrected Rows", corrected_rows)
            m5.metric("Failed Blocks", failed_count)

            tab1, tab2, tab3 = st.tabs(["Parsed Data", "Corrected Rows", "Failed Blocks"])

            with tab1:
                st.dataframe(df[DISPLAY_COLUMNS], use_container_width=True, height=520)

            with tab2:
                corrected_df = df[df["Correction Flag"] == "Yes"][DISPLAY_COLUMNS]
                if corrected_df.empty:
                    st.success("No corrected rows.")
                else:
                    st.dataframe(corrected_df, use_container_width=True, height=420)

            with tab3:
                if not failed_blocks:
                    st.success("No failed blocks.")
                else:
                    st.warning(
                        f"Parsed {total_rows} rows from {total_blocks} detected blocks. "
                        f"{failed_count} block(s) could not be parsed."
                    )
                    for idx, block in enumerate(failed_blocks, start=1):
                        st.text_area(f"Failed Block {idx}", block, height=120)

            excel_data = to_excel_bytes(df)
            st.download_button(
                label="Download Excel",
                data=excel_data,
                file_name="statement_output.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )

    except Exception as e:
        st.error(f"Error while processing PDF: {e}")
