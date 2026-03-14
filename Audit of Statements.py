import re
from io import BytesIO
import pandas as pd
import pdfplumber
import streamlit as st
import base64

st.set_page_config(page_title="Bank Statement Reader", page_icon="📄", layout="wide")

AG_IMAGE_PATH = "A.G(Audit).jpg"

DATE_START_RE = re.compile(r'^\s*(\d{2}-\d{2}-\d{4})')
DATE_ANY_RE = re.compile(r'(\d{2}-\d{2}-\d{4})')
BAL_RE = re.compile(r'(\d+(?:,\d{3})*\.\d{2}(?:Cr|Dr))')

HIGH_RISK_KEYWORDS = [
    "NEFT","IMPS","UPI","TRANSFER","TRF",
    "PVT","PRIVATE","TRADERS","ENTERPRISE",
    "AGENCY","SERVICES"
]

PERSON_NAME_WORDS = [
    "KUMAR","SINGH","SHARMA","GUPTA","VERMA",
    "DEVI","KAUR","PRASAD","ALI","KHAN"
]

DISPLAY_COLUMNS = [
    "Date","Description","Parsed Amount",
    "Debit","Credit","Closing Balance"
]

# ---------- Utilities ----------

def clean(text):
    return " ".join(str(text).split()) if text else ""

def balance_to_float(text):
    text = clean(text)
    if not text:
        return None
    sign = -1 if text.endswith("Dr") else 1
    num = text.replace("Cr","").replace("Dr","").replace(",","")
    try:
        return sign * float(num)
    except:
        return None

def amount_to_float(x):
    try:
        return float(str(x).replace(",",""))
    except:
        return None

def fmt(x):
    return f"{x:,.2f}"

# ---------- High Risk Detection ----------

def detect_high_risk(df):

    keyword_pattern = "|".join(HIGH_RISK_KEYWORDS)
    name_pattern = "|".join(PERSON_NAME_WORDS)
    human_name_regex = r'\b[A-Z]{3,}\s[A-Z]{3,}\b'

    high_debit = df[
        (df["Debit_num"]>0) &
        (
            df["Description"].str.upper().str.contains(keyword_pattern,na=False) |
            df["Description"].str.upper().str.contains(name_pattern,na=False) |
            df["Description"].str.upper().str.contains(human_name_regex,na=False)
        )
    ]

    high_credit = df[
        (df["Credit_num"]>0) &
        (
            df["Description"].str.upper().str.contains(keyword_pattern,na=False) |
            df["Description"].str.upper().str.contains(name_pattern,na=False) |
            df["Description"].str.upper().str.contains(human_name_regex,na=False)
        )
    ]

    return high_debit,high_credit


# ---------- PDF Parsing ----------

def build_blocks(file):
    blocks=[]
    current=""

    with pdfplumber.open(file) as pdf:
        for page in pdf.pages:
            text=page.extract_text()
            if not text:
                continue

            for line in text.split("\n"):
                line=clean(line)

                if DATE_START_RE.match(line):
                    if current:
                        blocks.append(current)
                    current=line
                else:
                    current+=" "+line

    if current:
        blocks.append(current)

    return blocks


def parse_block(block):

    date_match=DATE_ANY_RE.search(block)
    if not date_match:
        return None

    date=date_match.group(1)

    balances=BAL_RE.findall(block)
    if not balances:
        return None

    closing=balances[-1]

    amt_match=re.search(r'(\d+(?:,\d{3})*\.\d{2})',block)
    if not amt_match:
        return None

    amount=amt_match.group(1)

    desc=block.replace(date,"").replace(amount,"").replace(closing,"")

    return date,clean(desc),amount,closing


def process_pdf(file,opening_balance=None):

    blocks=build_blocks(file)
    rows=[]
    prev=opening_balance

    for block in blocks:

        result=parse_block(block)

        if not result:
            continue

        date,desc,amt,closing=result

        amt_float=amount_to_float(amt)
        closing_float=balance_to_float(closing)

        debit=0
        credit=0

        if prev is not None and closing_float is not None:

            diff=round(closing_float-prev,2)

            if diff<0:
                debit=abs(diff)
            else:
                credit=abs(diff)

        rows.append([
            date,
            desc,
            amt,
            debit,
            credit,
            closing
        ])

        prev=closing_float

    df=pd.DataFrame(rows,columns=DISPLAY_COLUMNS)

    df["Debit_num"]=pd.to_numeric(df["Debit"],errors="coerce").fillna(0)
    df["Credit_num"]=pd.to_numeric(df["Credit"],errors="coerce").fillna(0)

    return df


# ---------- Excel Export ----------

def to_excel(df):
    output=BytesIO()
    df.to_excel(output,index=False)
    output.seek(0)
    return output


# ---------- Header ----------

st.markdown("""
<div style="text-align:center">
<h1>📄 Bank Statement Reader</h1>
<p>Office Use Only</p>
<p>Supported Format: Jammu & Kashmir Bank Statement PDF</p>
</div>
""",unsafe_allow_html=True)


# ---------- Sidebar ----------

with st.sidebar:

    st.markdown(
        f"""
        <div style="text-align:center">
        <img src="data:image/jpg;base64,{base64.b64encode(open(AG_IMAGE_PATH,'rb').read()).decode()}" width="180">
        </div>
        """,
        unsafe_allow_html=True
    )

    st.header("About")

    st.write("Internal utility for bank statement review and audit analysis.")

# ---------- Upload ----------

file=st.file_uploader("Upload statement PDF",type=["pdf"])

opening_input=st.text_input("Opening Balance (example: 10000.00Cr)")

opening=balance_to_float(opening_input) if opening_input else None


# ---------- Main ----------

if file:

    with st.spinner("Processing PDF..."):

        df=process_pdf(file,opening)

        high_debit,high_credit=detect_high_risk(df)

    # ---------- Dashboard Metrics ----------

    rows=len(df)
    total_debit=df["Debit_num"].sum()
    total_credit=df["Credit_num"].sum()

    c1,c2,c3=st.columns(3)

    c1.metric("Transactions",rows)
    c2.metric("Total Debit",fmt(total_debit))
    c3.metric("Total Credit",fmt(total_credit))

    st.divider()

    st.subheader("Parsed Statement")

    st.dataframe(df,use_container_width=True)

    st.download_button(
        "Download Full Excel",
        data=to_excel(df),
        file_name="statement.xlsx"
    )

    st.divider()

    # ---------- High Risk Dashboard ----------

    st.subheader("High Risk Analysis")

    col1,col2=st.columns(2)

    with col1:

        st.markdown("### High Risk Debit")

        d_rows=len(high_debit)
        d_total=high_debit["Debit_num"].sum()

        st.metric("Rows",d_rows)
        st.metric("Amount",fmt(d_total))

        st.dataframe(high_debit,use_container_width=True)

        st.download_button(
            "Download High Risk Debit Excel",
            data=to_excel(high_debit),
            file_name="high_risk_debit.xlsx"
        )

    with col2:

        st.markdown("### High Risk Credit")

        c_rows=len(high_credit)
        c_total=high_credit["Credit_num"].sum()

        st.metric("Rows",c_rows)
        st.metric("Amount",fmt(c_total))

        st.dataframe(high_credit,use_container_width=True)

        st.download_button(
            "Download High Risk Credit Excel",
            data=to_excel(high_credit),
            file_name="high_risk_credit.xlsx"
        )


# ---------- Footer ----------

st.markdown("""
<div style="text-align:center;margin-top:40px">
Internal utility for bank statement review and audit analysis
</div>
""",unsafe_allow_html=True)
