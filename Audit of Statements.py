import re
from io import BytesIO
import pandas as pd
import pdfplumber
import streamlit as st
import base64

st.set_page_config(page_title="Bank Statement Parser", page_icon="📄", layout="wide")

AG_IMAGE_PATH = "A.G(Audit).jpg"

DATE_START_RE = re.compile(r'^\s*(\d{2}-\d{2}-\d{4})\b')
DATE_ANY_RE = re.compile(r'(\d{2}-\d{2}-\d{4})')
BAL_RE = re.compile(r'(\d+(?:,\d{3})*\.\d{2}(?:Cr|Dr))')
REF_CODE_RE = re.compile(r'\b[A-Z]{4}\d{6,}\b')

HIGH_RISK_KEYWORDS = [
    "NEFT","IMPS","UPI","TRANSFER","TRF",
    "PVT","PRIVATE","TRADERS","ENTERPRISE",
    "AGENCY","SERVICES"
]

PERSON_NAME_WORDS = [
    "KUMAR","SINGH","SHARMA","GUPTA","VERMA","DEVI","KAUR","LAL",
    "PRASAD","RAJ","ALI","KHAN","DAS","CHAND","YADAV","MEENA"
]

DISPLAY_COLUMNS = [
    "Date","Description","IFSC / Ref No","Parsed Amount",
    "Debit","Credit","Closing Balance","Correction Flag","Correction Note"
]

# ----------------------------------------------------------
# High Risk Detection
# ----------------------------------------------------------

def detect_high_risk(df):

    keyword_pattern = "|".join(HIGH_RISK_KEYWORDS)
    name_pattern = "|".join(PERSON_NAME_WORDS)

    human_name_regex = r'\b[A-Z]{3,}\s[A-Z]{3,}\b'

    high_risk_debit = df[
        (df["Debit_num"] > 0) &
        (
            df["Description"].str.upper().str.contains(keyword_pattern, na=False)
            |
            df["Description"].str.upper().str.contains(name_pattern, na=False)
            |
            df["Description"].str.upper().str.contains(human_name_regex, na=False)
        )
    ]

    high_risk_credit = df[
        (df["Credit_num"] > 0) &
        (
            df["Description"].str.upper().str.contains(keyword_pattern, na=False)
            |
            df["Description"].str.upper().str.contains(name_pattern, na=False)
            |
            df["Description"].str.upper().str.contains(human_name_regex, na=False)
        )
    ]

    return high_risk_debit, high_risk_credit


# ----------------------------------------------------------
# Utility Functions
# ----------------------------------------------------------

def clean(text):
    return " ".join(str(text).split()) if text else ""

def balance_to_float(balance_text):
    balance_text = clean(balance_text)

    if not balance_text:
        return None

    sign = -1 if balance_text.endswith("Dr") else 1
    num = balance_text.replace("Cr","").replace("Dr","").replace(",","")

    try:
        return sign * float(num)
    except:
        return None


def amount_to_float(x):
    try:
        return float(str(x).replace(",",""))
    except:
        return None


def fmt_amount(x):
    if x is None:
        return "0"
    return f"{float(x):.2f}"


# ----------------------------------------------------------
# PDF Parsing
# ----------------------------------------------------------

def build_transaction_blocks(file_obj):

    blocks = []
    current = ""

    with pdfplumber.open(file_obj) as pdf:
        for page in pdf.pages:

            text = page.extract_text()

            if not text:
                continue

            for line in text.split("\n"):

                line = clean(line)

                if DATE_START_RE.match(line):

                    if current:
                        blocks.append(current)

                    current = line

                else:
                    current += " " + line

    if current:
        blocks.append(current)

    return blocks


def parse_transaction_block(block):

    m_date = DATE_ANY_RE.search(block)

    if not m_date:
        return None

    date = m_date.group(1)

    balances = BAL_RE.findall(block)

    if not balances:
        return None

    closing_balance = balances[-1]

    amount_match = re.search(r'(\d+(?:,\d{3})*\.\d{2})', block)

    if not amount_match:
        return None

    amount = amount_match.group(1)

    desc = block.replace(date,"").replace(amount,"").replace(closing_balance,"")

    return {
        "date":date,
        "description":clean(desc),
        "ref_code":"",
        "amount":amount,
        "closing_balance":closing_balance
    }


# ----------------------------------------------------------
# PDF Processing
# ----------------------------------------------------------

def process_pdf(file_obj, opening_balance=None):

    blocks = build_transaction_blocks(file_obj)

    parsed_rows = []

    prev_balance = opening_balance

    for block in blocks:

        row = parse_transaction_block(block)

        if not row:
            continue

        date = row["date"]
        desc = row["description"]
        amount = amount_to_float(row["amount"])
        closing = balance_to_float(row["closing_balance"])

        debit = "0"
        credit = "0"

        if prev_balance is not None and closing is not None:

            delta = round(closing - prev_balance,2)

            if delta < 0:
                debit = fmt_amount(abs(delta))
            else:
                credit = fmt_amount(abs(delta))

        parsed_rows.append([
            date,
            desc,
            "",
            row["amount"],
            debit,
            credit,
            row["closing_balance"],
            "No",
            ""
        ])

        prev_balance = closing

    df = pd.DataFrame(parsed_rows, columns=DISPLAY_COLUMNS)

    if not df.empty:

        df["Debit_num"] = pd.to_numeric(df["Debit"], errors="coerce").fillna(0)
        df["Credit_num"] = pd.to_numeric(df["Credit"], errors="coerce").fillna(0)

    return df


# ----------------------------------------------------------
# Excel Export
# ----------------------------------------------------------

def to_excel_bytes(df):

    output = BytesIO()

    export_df = df.drop(columns=["Debit_num","Credit_num"], errors="ignore")

    with pd.ExcelWriter(output, engine="openpyxl") as writer:

        export_df.to_excel(writer, index=False)

    output.seek(0)

    return output


# ----------------------------------------------------------
# Header
# ----------------------------------------------------------

st.markdown("""
<div style="text-align:center">
<h1>📄 Bank Statement Reader</h1>
<p>Office Use Only</p>
<p>Supported Format: Jammu & Kashmir Bank Statement PDF</p>
</div>
""", unsafe_allow_html=True)


# ----------------------------------------------------------
# Sidebar
# ----------------------------------------------------------

with st.sidebar:

    st.markdown(
        f"""
        <div style="text-align:center">
        <img src="data:image/jpg;base64,{base64.b64encode(open(AG_IMAGE_PATH,"rb").read()).decode()}" width="180">
        </div>
        """,
        unsafe_allow_html=True
    )

    st.header("About")

    st.write("Internal utility for bank statement review and audit analysis.")

# ----------------------------------------------------------
# Upload
# ----------------------------------------------------------

uploaded_file = st.file_uploader("Upload statement PDF", type=["pdf"])

opening_balance_input = st.text_input(
"Enter Opening Balance (example: 10000.00Cr)"
)

opening_balance = balance_to_float(opening_balance_input) if opening_balance_input else None


# ----------------------------------------------------------
# Processing
# ----------------------------------------------------------

if uploaded_file:

    with st.spinner("Processing PDF..."):

        df = process_pdf(uploaded_file, opening_balance)

        high_debit, high_credit = detect_high_risk(df)

    st.dataframe(df[DISPLAY_COLUMNS], use_container_width=True)

    excel_data = to_excel_bytes(df)

    st.download_button(
        "Download Full Excel",
        data=excel_data,
        file_name="statement_output.xlsx"
    )

    st.subheader("High Risk Analysis")

    col1,col2 = st.columns(2)

    with col1:

        st.write("High Risk Debit")

        st.dataframe(high_debit[DISPLAY_COLUMNS])

        st.download_button(
            "Download High Risk Debit Excel",
            data=to_excel_bytes(high_debit),
            file_name="high_risk_debit.xlsx"
        )

    with col2:

        st.write("High Risk Credit")

        st.dataframe(high_credit[DISPLAY_COLUMNS])

        st.download_button(
            "Download High Risk Credit Excel",
            data=to_excel_bytes(high_credit),
            file_name="high_risk_credit.xlsx"
        )

# ----------------------------------------------------------
# Footer
# ----------------------------------------------------------

st.markdown("""
<div style="text-align:center;margin-top:40px">
Internal utility for bank statement review and audit analysis.
</div>
""", unsafe_allow_html=True)
