import streamlit as st
import pandas as pd
import numpy as np
import base64
import json
from google import genai
from google.genai.errors import APIError

# --- Cấu hình Trang Streamlit ---
st.set_page_config(
    page_title="App Đánh giá Phương án Kinh doanh",
    layout="wide"
)

st.title("Ứng dụng Đánh giá Hiệu quả Dự án (NPV, IRR, PP, DPP) 💰")
st.markdown("Sử dụng AI để trích xuất thông số tài chính từ file Word (.docx) và đánh giá dự án.")

# --- Thiết lập Session State để lưu trữ dữ liệu ---
if 'extracted_params' not in st.session_state:
    st.session_state.extracted_params = None
if 'project_metrics' not in st.session_state:
    st.session_state.project_metrics = None

# --- Khóa API (Lấy từ Streamlit Secrets) ---
GEMINI_API_KEY = st.secrets.get("GEMINI_API_KEY")

# --- Helper: Chuyển đổi Bytes sang Base64 ---
def bytes_to_base64(byte_data):
    """Chuyển đổi dữ liệu bytes thành chuỗi base64."""
    return base64.b64encode(byte_data).decode('utf-8')

# --- Helper: Tính thời gian hoàn vốn (PP) và hoàn vốn có chiết khấu (DPP) ---
def calculate_payback_periods(cash_flows, discount_rate):
    """Tính Payback Period (PP) và Discounted Payback Period (DPP)."""
    
    T = len(cash_flows) - 1 # Số năm dự án (từ năm 1)
    initial_investment = -cash_flows[0]
    
    # 1. PP (Thời gian hoàn vốn)
    cumulative_cf = np.cumsum(cash_flows[1:])
    pp = 0.0
    for i in range(T):
        if cumulative_cf[i] >= initial_investment:
            # Hoàn vốn trong năm thứ i+1
            # pp = i + 1 + (Vốn còn thiếu / CF năm i+1)
            remaining_capital = initial_investment - (cumulative_cf[i-1] if i > 0 else 0)
            pp = (i + 1) + (remaining_capital / cash_flows[i+1])
            break
        elif i == T - 1:
            pp = float('inf') # Dự án không hoàn vốn

    # 2. DPP (Thời gian hoàn vốn có chiết khấu)
    discounted_cf = cash_flows[1:] / (1 + discount_rate) ** np.arange(1, T + 1)
    cumulative_dcf = np.cumsum(discounted_cf)
    dpp = 0.0
    for i in range(T):
        if cumulative_dcf[i] >= initial_investment:
            # Hoàn vốn trong năm thứ i+1
            # dpp = i + 1 + (Vốn chiết khấu còn thiếu / DCF năm i+1)
            remaining_d_capital = initial_investment - (cumulative_dcf[i-1] if i > 0 else 0)
            dpp = (i + 1) + (remaining_d_capital / discounted_cf[i])
            break
        elif i == T - 1:
            dpp = float('inf') # Dự án không hoàn vốn

    return pp, dpp

# --- Chức năng 1: Trích xuất thông số tài chính từ DOCX bằng AI (Sử dụng JSON Schema) ---
def extract_financial_params(base64_data, api_key):
    """Sử dụng Gemini để trích xuất thông số tài chính từ file DOCX."""
    if not api_key:
        return "Lỗi API: Không tìm thấy Khóa API 'GEMINI_API_KEY'."

    st.warning("Xin lưu ý: Việc trích xuất sẽ hiệu quả nhất nếu các chỉ số được đề cập rõ ràng trong tài liệu Word.")

    try:
        client = genai.Client(api_key=api_key)
        
        prompt = """
        Bạn là một chuyên gia phân tích tài chính. Hãy trích xuất các thông số sau từ tài liệu Word được cung cấp. 
        Đơn vị tiền tệ (Vốn, Doanh thu, Chi phí) là VND và phải được định dạng dưới dạng số nguyên (integer). 
        Đơn vị của WACC và Thuế là tỷ lệ (ví dụ: 10% là 0.1). 
        Đơn vị của Dòng đời dự án là số năm (integer). 
        Nếu không tìm thấy bất kỳ thông số nào, hãy đặt giá trị là 0.
        """

        # Định nghĩa JSON Schema để buộc AI trả về cấu trúc dữ liệu mong muốn
        response_schema = {
            "type": "OBJECT",
            "properties": {
                "initialInvestment": {"type": "NUMBER", "description": "Tổng vốn đầu tư ban đầu (tại năm 0) (VND)"},
                "projectLifespan": {"type": "INTEGER", "description": "Dòng đời dự án theo năm"},
                "annualRevenue": {"type": "NUMBER", "description": "Doanh thu hàng năm (giả định cố định) (VND)"},
                "annualCost": {"type": "NUMBER", "description": "Chi phí hoạt động hàng năm (giả định cố định) (VND)"},
                "wacc": {"type": "NUMBER", "description": "Tỷ lệ Chi phí vốn bình quân (WACC - Dạng thập phân, ví dụ 0.1)"},
                "taxRate": {"type": "NUMBER", "description": "Thuế suất doanh nghiệp (Dạng thập phân, ví dụ 0.2)"}
            },
            "required": ["initialInvestment", "projectLifespan", "annualRevenue", "annualCost", "wacc", "taxRate"]
        }
        
        # Tạo Payload API
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": prompt},
                        {
                            "inlineData": {
                                "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                                "data": base64_data
                            }
                        }
                    ]
                }
            ],
            "config": {
                "response_mime_type": "application/json",
                "response_schema": response_schema,
            },
            "model": "gemini-2.5-flash-preview-05-20"
        }

        # Gọi API
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=payload['contents'],
            config=payload['config']
        )
        
        # Phân tích kết quả JSON
        json_text = response.text.strip()
        return json.loads(json_text)

    except APIError as e:
        return {"error": f"Lỗi gọi Gemini API: Vui lòng kiểm tra Khóa API. Chi tiết lỗi: {e}"}
    except json.JSONDecodeError:
        return {"error": "Lỗi phân tích JSON từ AI. Vui lòng thử lại với tài liệu rõ ràng hơn."}
    except Exception as e:
        return {"error": f"Đã xảy ra lỗi không xác định: {e}"}

# --- Chức năng 2 & 3: Xây dựng dòng tiền và tính toán chỉ số ---
@st.cache_data
def calculate_metrics(params):
    """Xây dựng bảng dòng tiền và tính toán các chỉ số NPV, IRR, PP, DPP."""
    
    # Ép kiểu dữ liệu
    try:
        I = float(params['initialInvestment'])
        T = int(params['projectLifespan'])
        R = float(params['annualRevenue'])
        C = float(params['annualCost'])
        WACC = float(params['wacc'])
        Tax = float(params['taxRate'])
    except:
        raise ValueError("Dữ liệu trích xuất không hợp lệ hoặc bị thiếu.")

    if T <= 0:
        raise ValueError("Dòng đời dự án phải lớn hơn 0.")
    if WACC <= 0:
        st.warning("WACC được đặt là 0 hoặc âm. Sử dụng 10% (0.1) làm tỷ lệ chiết khấu mặc định.")
        WACC = 0.1
    
    # 1. Tính toán Dòng tiền (CF - Cash Flow)
    # Giả định: (Doanh thu - Chi phí) là EBITDA. CF = (EBITDA * (1 - Thuế)) + Khấu hao (giả định = 0)
    # Đây là mô hình Cash Flow đơn giản nhất (sau thuế, chưa tính khấu hao/vốn lưu động)
    annual_net_cf = (R - C) * (1 - Tax) 

    # Dòng tiền năm 0 (Initial Investment)
    cash_flows = [-I]
    
    # Dòng tiền các năm 1 đến T
    cash_flows.extend([annual_net_cf] * T)
    
    # Chuyển đổi sang mảng numpy để tính toán tài chính
    cf_array = np.array(cash_flows)
    
    # 2. Tính NPV (Net Present Value)
    # Numpy npv tính từ năm 1, nên phải cộng thêm CF năm 0 (-I)
    npv = np.npv(WACC, cf_array[1:]) + cf_array[0] 
    
    # 3. Tính IRR (Internal Rate of Return)
    irr = np.irr(cf_array)
    
    # 4. Tính PP và DPP
    pp, dpp = calculate_payback_periods(cf_array, WACC)
    
    # Trả về kết quả
    results = {
        'cash_flows': pd.DataFrame({'Năm': list(range(T + 1)), 'Dòng Tiền (CF)': cash_flows}),
        'metrics': {
            'NPV': npv,
            'IRR': irr,
            'PP': pp,
            'DPP': dpp,
            'WACC': WACC
        }
    }
    return results

# --- Chức năng 4: Yêu cầu AI phân tích các chỉ số ---
def get_ai_analysis(metrics, api_key):
    """Gửi các chỉ số đánh giá dự án đến Gemini API và nhận phân tích."""
    if not api_key:
        return "Lỗi API: Không tìm thấy Khóa API 'GEMINI_API_KEY'."
    try:
        client = genai.Client(api_key=api_key)
        
        # Chuyển đổi float('inf') thành chuỗi thân thiện cho AI
        irr_str = f"{metrics['IRR'] * 100:.2f}%"
        pp_str = f"{metrics['PP']:.2f} năm" if metrics['PP'] != float('inf') else "Không hoàn vốn"
        dpp_str = f"{metrics['DPP']:.2f} năm" if metrics['DPP'] != float('inf') else "Không hoàn vốn"

        data_for_ai = f"""
        WACC: {metrics['WACC'] * 100:.2f}%
        NPV (Giá trị hiện tại ròng): {metrics['NPV']:,.0f} VND
        IRR (Tỷ suất sinh lời nội bộ): {irr_str}
        PP (Thời gian hoàn vốn): {pp_str}
        DPP (Thời gian hoàn vốn có chiết khấu): {dpp_str}
        """

        prompt = f"""
        Bạn là một chuyên gia phân tích tài chính dự án hàng đầu. Dựa trên các chỉ số sau của dự án, hãy đưa ra một đánh giá chi tiết và kết luận. 
        Đánh giá cần bao gồm:
        1. Nhận xét về NPV: Dự án có đáng đầu tư không? (NPV > 0)
        2. Nhận xét về IRR so với WACC: Dự án có khả thi không? (IRR > WACC)
        3. Nhận xét về PP và DPP: Khả năng thu hồi vốn nhanh hay chậm.
        4. Tóm tắt và đưa ra khuyến nghị cuối cùng (Chấp nhận hay Từ chối dự án).

        Dữ liệu chỉ số:
        {data_for_ai}
        """

        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt
        )
        return response.text

    except APIError as e:
        return f"Lỗi gọi Gemini API: Vui lòng kiểm tra Khóa API hoặc giới hạn sử dụng. Chi tiết lỗi: {e}"
    except Exception as e:
        return f"Đã xảy ra lỗi không xác định khi phân tích: {e}"

# --- Giao diện Streamlit ---

# 1. Tải File
uploaded_file = st.file_uploader(
    "1. Tải file Word (.docx) chứa phương án kinh doanh:",
    type=['docx']
)

# Nút Kích hoạt Trích xuất Dữ liệu
if uploaded_file is not None:
    if st.button("Tạo tác Lọc Dữ liệu (AI Extraction)", type="primary"):
        if GEMINI_API_KEY:
            with st.spinner('Đang gửi file Word cho AI để trích xuất thông số...'):
                # Chuyển bytes file DOCX thành base64
                docx_bytes = uploaded_file.read()
                base64_data = bytes_to_base64(docx_bytes)
                
                # Trích xuất
                params = extract_financial_params(base64_data, GEMINI_API_KEY)
                
                if isinstance(params, dict) and 'error' not in params:
                    st.session_state.extracted_params = params
                    st.success("Trích xuất thông số thành công!")
                else:
                    st.error(f"Lỗi trích xuất: {params.get('error', 'Không thể trích xuất thông số.')}")
        else:
            st.error("Lỗi: Không tìm thấy Khóa API. Vui lòng cấu hình Khóa 'GEMINI_API_KEY' trong Streamlit Secrets.")

# --- Hiển thị kết quả Trích xuất ---
if st.session_state.extracted_params:
    params = st.session_state.extracted_params
    st.divider()
    st.subheader("2. Các Thông số Tài chính được Trích xuất:")
    
    # Hiển thị tham số dưới dạng DataFrame
    params_df = pd.DataFrame([
        {"Chỉ tiêu": "Vốn đầu tư ban đầu (VND)", "Giá trị": f"{params['initialInvestment']:,.0f}"},
        {"Chỉ tiêu": "Dòng đời dự án (Năm)", "Giá trị": f"{params['projectLifespan']}"},
        {"Chỉ tiêu": "Doanh thu hàng năm (VND)", "Giá trị": f"{params['annualRevenue']:,.0f}"},
        {"Chỉ tiêu": "Chi phí hàng năm (VND)", "Giá trị": f"{params['annualCost']:,.0f}"},
        {"Chỉ tiêu": "WACC (Tỷ lệ chiết khấu)", "Giá trị": f"{params['wacc'] * 100:.2f}%"},
        {"Chỉ tiêu": "Thuế suất doanh nghiệp", "Giá trị": f"{params['taxRate'] * 100:.2f}%"},
    ])
    st.table(params_df)

    # --- Thực hiện tính toán và hiển thị Dòng tiền + Chỉ số ---
    try:
        results = calculate_metrics(params)
        st.session_state.project_metrics = results['metrics']
        
        st.subheader("3. Bảng Dòng tiền (Cash Flow) của Dự án")
        # Định dạng và hiển thị bảng dòng tiền
        cf_df = results['cash_flows']
        st.dataframe(
            cf_df.style.format({'Dòng Tiền (CF)': '{:,.0f}'}), 
            use_container_width=True, 
            hide_index=True
        )

        st.subheader("4. Các Chỉ số Đánh giá Hiệu quả Dự án")
        metrics = results['metrics']
        
        col_npv, col_irr, col_pp, col_dpp = st.columns(4)

        with col_npv:
            # NPV
            st.metric(
                label="Giá trị Hiện tại Ròng (NPV)",
                value=f"{metrics['NPV']:,.0f} VND",
                delta="Dự án chấp nhận được" if metrics['NPV'] > 0 else "Dự án bị từ chối"
            )
        with col_irr:
            # IRR
            st.metric(
                label="Tỷ suất Sinh lời Nội bộ (IRR)",
                value=f"{metrics['IRR'] * 100:.2f}%"
            )
        with col_pp:
            # PP
            st.metric(
                label="Thời gian Hoàn vốn (PP)",
                value=f"{metrics['PP']:.2f} năm" if metrics['PP'] != float('inf') else "Không hoàn vốn"
            )
        with col_dpp:
            # DPP
            st.metric(
                label="Hoàn vốn có Chiết khấu (DPP)",
                value=f"{metrics['DPP']:.2f} năm" if metrics['DPP'] != float('inf') else "Không hoàn vốn"
            )
            
        st.caption(f"*Tỷ lệ chiết khấu WACC: {metrics['WACC'] * 100:.2f}%")

        # --- Yêu cầu AI Phân tích ---
        st.divider()
        st.subheader("5. Phân tích Chuyên sâu từ AI")
        
        if st.button("Yêu cầu AI Phân tích Hiệu quả Dự án", key='ai_analysis_button'):
            if GEMINI_API_KEY:
                with st.spinner('Đang gửi các chỉ số và chờ Gemini phân tích...'):
                    ai_result = get_ai_analysis(metrics, GEMINI_API_KEY)
                    st.markdown("**Kết quả Phân tích từ Gemini AI:**")
                    st.info(ai_result)
            else:
                st.error("Lỗi: Không tìm thấy Khóa API. Vui lòng cấu hình Khóa 'GEMINI_API_KEY' trong Streamlit Secrets.")


    except ValueError as ve:
        st.error(f"Lỗi Tính toán Dữ liệu: {ve}. Vui lòng kiểm tra các giá trị trích xuất từ AI.")
    except Exception as e:
        st.error(f"Đã xảy ra lỗi khi tính toán các chỉ số: {e}")

else:
    st.info("Vui lòng tải lên file Word và nhấn nút 'Tạo tác Lọc Dữ liệu' để bắt đầu.")
