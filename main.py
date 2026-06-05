import os
import smtplib
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

import fitz  # PyMuPDF
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, EmailStr, Field

load_dotenv()

TEMPLATE_PATH = Path(__file__).parent / "slip template.pdf"

GRAY_ROW_BG = (238 / 255, 238 / 255, 238 / 255)  # #EEEEEE
GRAY_ROW_PLACEHOLDERS = {"[SALARY_AMOUNT]", "[SALARY_WORDS]"}

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM_NAME = os.getenv("SMTP_FROM_NAME", "Buildberg Payroll")

app = FastAPI(title="Payslip Generator")


class PayslipRequest(BaseModel):
    employee_code: str = Field(..., description="Value for [EMPLOYEE_CODE]")
    employee_name: str = Field(..., description="Value for [EMPLOYEE_NAME]")
    employee_role: str = Field(..., description="Value for [EMPLOYEE_ROLE]")
    salary: str = Field(..., description="Value for [SALARY] (earnings row)")
    salary_amount: str = Field(..., description="Value for [SALARY_AMOUNT] (total pay)")
    salary_words: str = Field(..., description="Value for [SALARY_WORDS] (total pay in words)")
    email: EmailStr = Field(..., description="Recipient email address")


def _color_int_to_rgb(c: int) -> tuple[float, float, float]:
    return ((c >> 16) & 0xFF) / 255, ((c >> 8) & 0xFF) / 255, (c & 0xFF) / 255


def fill_pdf(template_bytes: bytes, replacements: dict[str, str]) -> bytes:
    doc = fitz.open(stream=template_bytes, filetype="pdf")
    page = doc[0]
    page_width = page.rect.width

    text_dict = page.get_text("dict")
    actions = []

    for block in text_dict.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                original = span["text"]
                matched = [ph for ph in replacements if ph in original]
                if not matched:
                    continue

                new_text = original
                for ph, val in replacements.items():
                    new_text = new_text.replace(ph, str(val))

                bbox = fitz.Rect(span["bbox"])
                # Right-aligned spans in this template sit near the right margin.
                right_aligned = (page_width - bbox.x1) < 60
                is_bold = "Bold" in span.get("font", "")
                fill = GRAY_ROW_BG if any(ph in GRAY_ROW_PLACEHOLDERS for ph in matched) else (1, 1, 1)

                actions.append(
                    {
                        "bbox": bbox,
                        "text": new_text,
                        "size": span["size"],
                        "color": _color_int_to_rgb(span.get("color", 0)),
                        "right_aligned": right_aligned,
                        "fontname": "hebo" if is_bold else "helv",
                        "fill": fill,
                    }
                )

    for a in actions:
        rect = fitz.Rect(a["bbox"])
        rect.x0 -= 1
        rect.x1 += 1
        page.add_redact_annot(rect, fill=a["fill"])
    page.apply_redactions()

    for a in actions:
        bbox = a["bbox"]
        text_width = fitz.get_text_length(a["text"], fontsize=a["size"], fontname=a["fontname"])
        x = bbox.x1 - text_width if a["right_aligned"] else bbox.x0
        y = bbox.y1 - 2  # baseline near the bottom of the original span
        page.insert_text(
            fitz.Point(x, y),
            a["text"],
            fontsize=a["size"],
            color=a["color"],
            fontname=a["fontname"],
        )

    output = doc.tobytes()
    doc.close()
    return output


def send_payslip_email(to_email: str, employee_name: str, pdf_bytes: bytes) -> None:
    if not SMTP_USER or not SMTP_PASSWORD:
        raise RuntimeError("SMTP_USER and SMTP_PASSWORD must be set in environment")

    msg = EmailMessage()
    msg["Subject"] = f"Payslip - {employee_name}"
    msg["From"] = f"{SMTP_FROM_NAME} <{SMTP_USER}>"
    msg["To"] = to_email
    msg.set_content(
        f"Hi {employee_name},\n\n"
        f"Please find your payslip attached.\n\n"
        f"Regards,\nBuildberg PTY LTD"
    )
    msg.add_attachment(
        pdf_bytes,
        maintype="application",
        subtype="pdf",
        filename=f"payslip-{employee_name.replace(' ', '_')}.pdf",
    )

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
        smtp.starttls()
        smtp.login(SMTP_USER, SMTP_PASSWORD)
        smtp.send_message(msg)


@app.post("/generate-payslip")
def generate_payslip(req: PayslipRequest):
    if not TEMPLATE_PATH.exists():
        raise HTTPException(status_code=500, detail=f"Template not found at {TEMPLATE_PATH}")

    today = datetime.now()
    replacements = {
        "[EMPLOYEE_CODE]": req.employee_code,
        "[EMPLOYEE_NAME]": req.employee_name,
        "[EMPLOYEE_ROLE]": req.employee_role,
        "[SALARY]": req.salary,
        "[SALARY_AMOUNT]": req.salary_amount,
        "[SALARY_WORDS]": req.salary_words,
        "[PAY_DATE]": today.strftime("%d %B %Y"),
        "[PAY_PERIOD]": today.strftime("%B %Y"),
    }

    template_bytes = TEMPLATE_PATH.read_bytes()
    pdf_bytes = fill_pdf(template_bytes, replacements)

    try:
        send_payslip_email(req.email, req.employee_name, pdf_bytes)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to send email: {e}")

    return {
        "status": "sent",
        "to": req.email,
        "pay_date": replacements["[PAY_DATE]"],
        "pay_period": replacements["[PAY_PERIOD]"],
    }


@app.get("/health")
def health():
    return {"ok": True, "template_present": TEMPLATE_PATH.exists()}
