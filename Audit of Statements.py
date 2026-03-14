import re
import os
import base64
from io import BytesIO
from datetime import datetime

import pandas as pd
import pdfplumber
import streamlit as st

# OCR imports (optional fallback)
try:
    import pytesseract
    from PIL import ImageOps, ImageFilter
    OCR_AVAILABLE = True
except Exception:
    OCR_AVAILABLE = False

st.set_page_config(page_title="Bank Statement Reader", page_icon="📄", layout="wide")

# -------------------- Constants --------------------
AG_IMAGE_PATH = "A.G(Audit).jpg"
USAGE_LOG_FILE = "app_usage_log.xlsx"
ADMIN_PASSWORD = "Audit@123"   # change this password

DATE_START_RE = re.compile(r'^\s*(\d{2}-\d{2}-\d{4})\b')
DATE_ANY_RE = re.compile(r'(\d{2}-\d{2}-\d{4})')
BAL_RE = re.compile(r'(\d+(?:,\d{3})*\.\d{2}(?:Cr|Dr))')
REF_CODE_RE = re.compile(r'\b[A-Z]{4}\d{6,}\b')

HIGH_RISK_KEYWORDS = [
    "NEFT", "IMPS", "UPI", "TRANSFER", "TRF",
    "PVT", "PRIVATE", "TRADERS", "ENTERPRISE",
    "AGENCY", "SERVICES"
]

PERSON_NAME_WORDS = [
    "KUMAR", "SINGH", "SHARMA", "GUPTA", "VERMA", "DEVI", "KAUR", "LAL",
    "PRASAD", "RAJ", "ALI", "KHAN", "DAS", "CHAND", "YADAV", "MEENA"
]

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

# -------------------- Utility Functions --------------------
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


def score_page_text(text):
    if not text:
        return 0
    lines = [clean(x) for x in text.split("\n") if clean(x)]
    date_starts = sum(1 for x in lines if DATE_START_RE.match(x))
    balances = len(BAL_RE.findall(text))
    dates_any = len(DATE_ANY_RE.findall(text))
    return (date_starts * 10) + (balances * 4) + dates_any + len(lines) * 0.05

# -------------------- OCR Functions --------------------
def preprocess_ocr_image(pil_img):
    img = pil_img.convert("L")
    img = ImageOps.autocontrast(img)
    img = img.filter(ImageFilter.SHARPEN)
    return img


def ocr_extract_page_text(page):
    if not OCR_AVAILABLE:
        return ""
    try:
        page_img = page.to_image(resolution=300).original
        page_img = preprocess_ocr_image(page_img)
        text = pytesseract.image_to_string(page_img, config="--psm 6")
        return text or ""
    except Exception:
        return ""


def get_best_page_text(page):
    extracted_text = page.extract_text() or ""
    extracted_score = score_page_text(extracted_text)

    if extracted_score >= 12:
        return extracted_text, False

    ocr_text = ocr_extract_page_text(page)
    ocr_score = score_page_text(ocr_text)

    if ocr_score > extracted_score:
        return ocr_text, True

    return extracted_text, False

# -------------------- Parser Functions --------------------
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
    ocr_used_pages = 0

    file_obj.seek(0)
    with pdfplumber.open(file_obj) as pdf:
        for page in pdf.pages:
            text, used_ocr = get_best_page_text(page)

            if used_ocr:
                ocr_used_pages += 1

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

    return blocks, ocr_used_pages


def process_pdf(file_obj, opening_balance=None):
    blocks, ocr_used_pages = build_transaction_blocks(file_obj)

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
    else:
        df["Debit_num"] = pd.Series(dtype=float)
        df["Credit_num"] = pd.Series(dtype=float)

    return df, failed_blocks, len(blocks), ocr_used_pages

# -------------------- High Risk Detection --------------------
def detect_high_risk(df):
    if df.empty:
        return df.copy(), df.copy()

    keyword_pattern = "|".join(HIGH_RISK_KEYWORDS)
    name_pattern = "|".join(PERSON_NAME_WORDS)
    human_name_regex = r'\b[A-Z]{3,}\s[A-Z]{3,}\b'
    exclude_regex = r'\b(BANK|JAMMU|KASHMIR|GOVT|GOVERNMENT|TREASURY|SECRETARIAT|ACCOUNT|SALARY|INTEREST|CHARGE|GST|TAX)\b'

    desc_upper = df["Description"].str.upper()

    likely_person_or_private = (
        desc_upper.str.contains(keyword_pattern, na=False, regex=True) |
        desc_upper.str.contains(name_pattern, na=False, regex=True) |
        (
            desc_upper.str.contains(human_name_regex, na=False, regex=True) &
            ~desc_upper.str.contains(exclude_regex, na=False, regex=True)
        )
    )

    high_risk_debit = df[(df["Debit_num"] > 0) & likely_person_or_private].copy()
    high_risk_credit = df[(df["Credit_num"] > 0) & likely_person_or_private].copy()

    return high_risk_debit, high_risk_credit

# -------------------- Excel Export --------------------
def to_excel_bytes(df, sheet_name="Statement"):
    output = BytesIO()
    export_df = df.drop(columns=["Debit_num", "Credit_num"], errors="ignore")

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        export_df.to_excel(writer, index=False, sheet_name=sheet_name)
        ws = writer.sheets[sheet_name]

        for idx, column_name in enumerate(export_df.columns, start=1):
            max_len = max(
                len(str(column_name)),
                *(len(str(v)) for v in export_df[column_name].fillna(""))
            )
            col_letter = ws.cell(row=1, column=idx).column_letter
            ws.column_dimensions[col_letter].width = min(max(max_len + 2, 12), 50)

    output.seek(0)
    return output

# -------------------- Usage Log --------------------
def log_user_usage_to_excel(
    name,
    email,
    section_field_party,
    file_name,
    total_rows,
    corrected_rows,
    failed_blocks,
    ocr_used_pages
):
    log_row = {
        "Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "Name": name,
        "Email": email,
        "Section / Field Party No.": section_field_party,
        "Uploaded File": file_name,
        "Total Rows": total_rows,
        "Corrected Rows": corrected_rows,
        "Failed Blocks": failed_blocks,
        "OCR Pages Used": ocr_used_pages,
    }

    new_df = pd.DataFrame([log_row])

    if os.path.exists(USAGE_LOG_FILE):
        try:
            existing_df = pd.read_excel(USAGE_LOG_FILE)
            updated_df = pd.concat([existing_df, new_df], ignore_index=True)
        except Exception:
            updated_df = new_df
    else:
        updated_df = new_df

    with pd.ExcelWriter(USAGE_LOG_FILE, engine="openpyxl") as writer:
        updated_df.to_excel(writer, index=False, sheet_name="Usage Log")
        ws = writer.sheets["Usage Log"]

        for idx, column_name in enumerate(updated_df.columns, start=1):
            max_len = max(
                len(str(column_name)),
                *(len(str(v)) for v in updated_df[column_name].fillna(""))
            )
            col_letter = ws.cell(row=1, column=idx).column_letter
            ws.column_dimensions[col_letter].width = min(max(max_len + 2, 15), 40)

# -------------------- Styles --------------------
st.markdown(
    """
    <style>
    .creator-footer {
        text-align: center;
        font-size: 15px;
        margin-top: 40px;
        padding-top: 15px;
        border-top: 1px solid #d9d9d9;
        color: #333333;
        font-weight: 500;
    }
    .audit-head {
        text-align:center;
        padding-top:10px;
        padding-bottom:10px;
    }
    .audit-title {
        font-size:40px;
        font-weight:700;
        margin-bottom:5px;
        color:#0f172a;
    }
    .audit-sub {
        font-size:18px;
        margin-bottom:2px;
        color:#444;
    }
    .audit-sub2 {
        font-size:16px;
        color:#666;
    }
    .access-box {
        background:#f8fafc;
        border:1px solid #e2e8f0;
        border-radius:10px;
        padding:14px;
        margin-bottom:12px;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# -------------------- Header --------------------
st.markdown(
    """
    <div class="audit-head">
        <div class="audit-title">📄 Bank Statement Reader</div>
        <div class="audit-sub">Office Use Only</div>
        <div class="audit-sub2">Supported Format: Jammu & Kashmir Bank Statement PDF Only</div>
    </div>
    """,
    unsafe_allow_html=True
)

# -------------------- Sidebar --------------------
with st.sidebar:
    try:
        st.markdown(
            f"""
            <div style="text-align: center;">
                <img src="data:image/jpg;base64,{base64.b64encode(open(AG_IMAGE_PATH, "rb").read()).decode()}" width="180">
            </div>
            """,
            unsafe_allow_html=True
        )
    except Exception:
        st.info("Sidebar logo not found.")

    st.header("About")
    st.write("Internal utility for bank statement review and audit analysis.")

    st.markdown("**Steps**")
    st.write("1. Fill user access details")
    st.write("2. Upload PDF")
    st.write("3. Enter Opening Balance")
    st.write("4. Review parsed data, corrected rows and failed blocks")
    st.write("5. Download Excel outputs")

    if not OCR_AVAILABLE:
        st.warning("OCR fallback is not available in this environment.")

    st.divider()
    st.subheader("Admin Access")
    admin_password = st.text_input("Admin Password", type="password")

    if admin_password == ADMIN_PASSWORD:
        st.success("Admin access granted")
        if os.path.exists(USAGE_LOG_FILE):
            with open(USAGE_LOG_FILE, "rb") as f:
                st.download_button(
                    label="Download Usage Log",
                    data=f,
                    file_name="app_usage_log.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True
                )
    elif admin_password:
        st.error("Invalid admin password")

# -------------------- User Access Details on Main Page --------------------
st.markdown('<div class="access-box">', unsafe_allow_html=True)
st.markdown("### User Access Details")

u1, u2, u3 = st.columns(3)

with u1:
    user_name = st.text_input("Your Name *", placeholder="Enter your full name")

with u2:
    user_email = st.text_input("Official Email ID *", placeholder="Enter official email")

with u3:
    user_section = st.text_input("Section / Field Party No. *", placeholder="Enter section or field party no.")

st.caption("These details are mandatory and recorded for internal monitoring and audit support.")
st.markdown('</div>', unsafe_allow_html=True)

if not (user_name.strip() and user_email.strip() and user_section.strip()):
    st.warning("Please fill Name, Email ID and Section / Field Party No. to use this app.")
    st.stop()

# -------------------- Inputs --------------------
uploaded_file = st.file_uploader("Upload statement PDF", type=["pdf"])

opening_balance_input = st.text_input(
    "Enter Opening Balance manually (example: 90817476.00Cr or 1250.00Dr)",
    value=""
)

opening_balance = balance_to_float(opening_balance_input) if opening_balance_input.strip() else None

if opening_balance_input.strip() and opening_balance is None:
    st.error("Invalid opening balance format. Use format like 90817476.00Cr or 1250.00Dr")
    st.stop()

# -------------------- Main --------------------
if uploaded_file is None:
    st.info("Upload a PDF to start.")
else:
    try:
        with st.spinner("Processing PDF..."):
            df, failed_blocks, total_blocks, ocr_used_pages = process_pdf(
                uploaded_file,
                opening_balance=opening_balance
            )
            high_debit, high_credit = detect_high_risk(df)

        if df.empty:
            st.error("No transactions could be parsed from the uploaded PDF.")
        else:
            total_rows = len(df)
            total_debit = float(df["Debit_num"].sum())
            total_credit = float(df["Credit_num"].sum())
            corrected_rows = int((df["Correction Flag"] == "Yes").sum())
            failed_count = len(failed_blocks)

            log_key = f"{user_email}_{uploaded_file.name}"
            if "last_logged_key" not in st.session_state:
                st.session_state["last_logged_key"] = ""

            if st.session_state["last_logged_key"] != log_key:
                log_user_usage_to_excel(
                    name=user_name,
                    email=user_email,
                    section_field_party=user_section,
                    file_name=uploaded_file.name,
                    total_rows=total_rows,
                    corrected_rows=corrected_rows,
                    failed_blocks=failed_count,
                    ocr_used_pages=ocr_used_pages
                )
                st.session_state["last_logged_key"] = log_key

            st.subheader("Statement Overview")
            m1, m2, m3, m4, m5, m6 = st.columns(6)
            m1.metric("Rows", total_rows)
            m2.metric("Total Debit", f"{total_debit:,.2f}")
            m3.metric("Total Credit", f"{total_credit:,.2f}")
            m4.metric("Corrected Rows", corrected_rows)
            m5.metric("Failed Blocks", failed_count)
            m6.metric("OCR Pages Used", ocr_used_pages)

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

            excel_data = to_excel_bytes(df, sheet_name="Statement")
            st.download_button(
                label="Download Full Excel",
                data=excel_data,
                file_name="statement_output.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )

            st.divider()
            st.subheader("High Risk Analysis")

            d_rows = len(high_debit)
            d_total = float(high_debit["Debit_num"].sum()) if not high_debit.empty else 0.0
            c_rows = len(high_credit)
            c_total = float(high_credit["Credit_num"].sum()) if not high_credit.empty else 0.0

            h1, h2, h3, h4 = st.columns(4)
            h1.metric("High Risk Debit Rows", d_rows)
            h2.metric("High Risk Debit Amount", f"{d_total:,.2f}")
            h3.metric("High Risk Credit Rows", c_rows)
            h4.metric("High Risk Credit Amount", f"{c_total:,.2f}")

            col1, col2 = st.columns(2)

            with col1:
                st.markdown("### High Risk Debit Entries")
                if not high_debit.empty:
                    st.dataframe(high_debit[DISPLAY_COLUMNS], use_container_width=True, height=380)
                    excel_high_debit = to_excel_bytes(high_debit, sheet_name="High Risk Debit")
                    st.download_button(
                        "Download High Risk Debit Excel",
                        data=excel_high_debit,
                        file_name="high_risk_debit.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        use_container_width=True
                    )
                else:
                    st.info("No high risk debit entries found.")

            with col2:
                st.markdown("### High Risk Credit Entries")
                if not high_credit.empty:
                    st.dataframe(high_credit[DISPLAY_COLUMNS], use_container_width=True, height=380)
                    excel_high_credit = to_excel_bytes(high_credit, sheet_name="High Risk Credit")
                    st.download_button(
                        "Download High Risk Credit Excel",
                        data=excel_high_credit,
                        file_name="high_risk_credit.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        use_container_width=True
                    )
                else:
                    st.info("No high risk credit entries found.")

    except Exception as e:
        st.error(f"Error while processing PDF: {e}")

# -------------------- Footer --------------------
st.markdown(
    """
    <div class="creator-footer">
        Internal utility for bank statement review and audit analysis.
    </div>
    """,
    unsafe_allow_html=True
)
