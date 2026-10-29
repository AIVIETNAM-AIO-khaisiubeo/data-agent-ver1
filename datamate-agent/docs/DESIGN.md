# DataMate Agent — thiết kế, cơ sở khoa học và kết quả

Tài liệu giải thích **mỗi thành phần lấy ý tưởng từ bài báo nào, lấy gì, bỏ gì và vì sao**, bám đề bài
*Zalo AI Challenge 2026 – DataMate*, luật chấm *Penalized Accuracy (PA)*, quy định dữ liệu (rulebook theo từng split,
business type mới ở private test) và ràng buộc kỹ thuật (≤ 15B tham số, offline, 6 giờ trên 2×RTX 5090, tái lập ±0.03 PA,
log từng câu). Tài liệu tham khảo: [`REFERENCES.md`](REFERENCES.md) (mã `[R#]`).

---

## 1. Dữ liệu thật nói gì

| Quan sát (train 3.000 câu / 30 shop, public 1.000 câu / 14 shop) | Hệ quả thiết kế |
|---|---|
| Kiểu đáp án: number 63%, percent 19%, list 6%, date 5%, string 3%, unanswerable 2.3%, boolean, table | Formatter đúng luật §9 của rulebook là điều kiện cần (sai format = −1) |
| Khoảng 73% câu thuộc **lõi bán hàng**: doanh thu, số đơn, huỷ, giá trị trung bình đơn, % thay đổi, ngày/tháng bán đắt nhất, top-k hàng/khách, tỉ trọng nhóm, số lượng theo đơn vị cơ bản (tỉ lệ tương tự ở public) | Lõi này được tính **deterministic** trên view chuẩn theo rulebook; LLM xử lý phần còn lại |
| Câu hỏi sinh từ khoảng 30 "họ ý định", diễn đạt lại kèm teencode và **lỗi gõ Telex** ("toongr", "dduwowcj", "thangs") | Bộ chuẩn hoá `qnorm`: bỏ dấu + sửa Telex + bung viết tắt, dùng chung cho mọi bộ phân tích |
| Hơn 40 cách tổ chức file: CSV phẳng, Excel mỗi tháng một sheet, mỗi tháng một file, header hai tầng, dòng tiêu đề, dòng "Cộng", ngày trộn `dd/mm/yyyy` · serial Excel · `1 thg 6 2025` · `1/6` (năm lấy từ sheet), tiền `45k` · `45.000đ` · `6,719.0000`, số lượng `1 hộp 30`, SQLite tên camelCase, cột tiếng Anh | Loader tổng quát (mục 4.1); test có cả tên cột tiếng Anh |
| README có **từ điển cột** với mô tả khá chuẩn ("thành tiền sau giảm", "trạng thái đơn"…) nhưng chất lượng không đều | Gán vai trò cột dựa trên README trước, tên cột sau (mục 4.2) |
| Rulebook **khác nhau giữa các split**: public thêm luật ngành (Mẹ & bé, Sửa xe, Điện thoại), bỏ luật "giá nhập bình quân", thêm "nhà cung cấp chính" | Rulebook đưa nguyên văn vào prompt; code đọc quy ước từ văn bản; template chỉ chạy khi gặp đúng điều luật nó cài (mục 4.4) |
| Câu `unanswerable` (70 câu ở train): kỳ nằm ngoài dữ liệu, thuộc tính không được ghi (giới tính, địa chỉ, lý do huỷ, kênh, tồn kho, nhập hàng, giá vốn…), câu chủ quan | Bộ kiểm tra khả năng trả lời dựa trên bằng chứng dương (mục 4.9) |

## 2. Quy tắc quyết định tối ưu cho PA

Với *p* = P(đúng | x): trả lời cho kỳ vọng *2p − 1*, abstain cho 0, nên **trả lời ⇔ p > 0.5**. Đây là reject option
của Chow [R27] (selective classification [R28]). `unanswerable` cũng là một câu trả lời nên chịu cùng quy tắc.
Toàn bộ hệ thống phục vụ việc ước lượng *p* cho chuẩn: calibration [R29] bằng logistic/Platt [R30] cộng isotonic [R31],
fit với **cross-validation nhóm theo shop**, để con số báo cáo phản ánh shop chưa từng thấy (private có business type mới).

## 3. Kiến trúc

```
/data/<split> ─ rulebook.md ─► RuleBook: văn bản (prompt) · quy ước (code) · dấu vân tay điều luật
             ─ calendar.csv ─► LunarCalendar (fallback: thuật toán Hồ Ngọc Đức)
             ─ data/<shop>/ ─► Loader ─► Tables (DuckDB) ─► README dictionary ─► Roles ─► Views:
                                                         v_sales · v_orders · v_products · v_purchases (+ self-checks)
question ─► qnorm ─► Analyzer (intent, type, k, ngưỡng, bộ lọc) · TimeResolver · ValueLinker · Answerability
        ─► Candidates:  templates (0 ms) ──────────────┐
                        LLM vòng 1: SQL ×3 (batch) ────┤ early-exit nếu ≥3 đồng thuận (và khớp template)
                        LLM vòng 2: plan→SQL, pandas,  ┤
                                    ReAct, tự sửa      ┘
        ─► Gom cụm theo đáp án đã thực thi ─► bỏ phiếu (+ judge) ─► Calibrator ─► Chow ─► submission + log
```

## 4. Thành phần và nguồn gốc

### 4.1 Nạp dữ liệu → DuckDB  (`loader.py`)
* DuckDB [R34] làm engine; mỗi shop một connection in-process.
* Spider 2.0 [R2] cho thấy prompt-only sụp đổ trên dữ liệu doanh nghiệp (DAIL-SQL + GPT-4o chỉ 5.68% trên Spider 2.0-lite
  so với 57.4% trên BIRD [R1]). Vì vậy dữ liệu phải được **làm sạch trước khi đặt câu hỏi**:
  * dò header (bỏ dòng tiêu đề, dùng hàng nhãn nhóm để điền ô header trống), bỏ chú thích cuối sheet, dòng header lặp;
  * bỏ dòng "Cộng ngày …/TỔNG NGÀY …": nhãn phải là **ô chữ đầu tiên** của dòng (số tiền tổng bị bỏ qua), nên khách tên
    "Tống Nam Minh" ở cột sau không bị nhầm là dòng tổng (lỗi này từng làm mất đơn thật ở 5 shop);
  * gộp các sheet/file cùng tập cột (mỗi tháng một sheet hoặc một file) thành một bảng, có cột `sheet`; tên bảng lấy theo
    **tên sheet** khi một workbook chứa nhiều danh sách khác nhau, để phân loại bảng không bị tên file chi phối;
  * đọc giá trị: tiền (`1tr440`, `45k`), số lượng kèm đơn vị (`1 ly`, `15 vỉ`), ngày trộn nhiều định dạng (năm của sheet cho
    `1/6`), giờ, boolean (chỉ khi có chữ dạng có/không; cột toàn số như `Giảm (đ)` = 0, 2.000 là số);
  * đọc xlsx bằng calamine (nhanh gấp ~4 lần openpyxl), cache theo kích thước và mtime file.

### 4.2 Từ điển README và gán vai trò cột  (`readme.py`, `roles.py`)
* Schema linking là bước lõi của RESDSQL [R6], CHESS [R7] và CHASE-SQL [R8]. Schema mỗi shop nhỏ nên ta **gán vai trò cho
  mọi cột** thay vì cắt bớt schema. Bằng chứng theo thứ tự: mô tả cột trong README → tên cột (Việt/Anh/camelCase/viết tắt) →
  kiểu giá trị. Mô tả README được khớp theo **cụm từ mở đầu** ("hệ số: 1 đơn vị lớn = …" là hệ số, không phải đơn vị lớn);
  tên cột bỏ hậu tố tiền tệ trước khi khớp (`Thành tiền (đ)`, `Giảm (VNĐ)`).
* Bảng được phân loại theo **tên bảng/sheet trước, tên file sau**: bán hàng, nhập, tồn, lô, thanh toán, giao hàng, công nợ,
  chi phí, trả hàng, bảo hành, ghi tắt, khách, nhà cung cấp, sản phẩm, quy đổi, nhân viên. Cột "tên" mang nghĩa theo loại
  bảng: tên hàng trong danh mục, tên khách trong danh sách khách, tên nhân viên trong danh sách nhân viên.

### 4.3 View chuẩn theo rulebook  (`salesview.py`)
* Từ các bảng thô dựng `v_sales` (mỗi dòng hàng), `v_orders` (mỗi đơn), `v_products`, `v_purchases`. Các luật áp **một lần** tại đây:
  đơn hợp lệ = không huỷ, không trả (trả một phần cũng bỏ cả đơn; dòng âm, cột "SL trả" > 0, ghi chú "khách trả lại" đều là trả);
  doanh thu = tổng thành tiền dòng (voucher/điểm/phí ship không tính, "Phí phục vụ" tính doanh thu nhưng không là mặt hàng);
  số lượng quy về đơn vị cơ bản (bảng quy đổi, cột "Quy đổi" dạng văn bản, hoặc dòng quy đổi trong README); hệ số ghi theo
  đơn vị trung gian ("1 hộp = 15 vỉ", "1 vỉ = 12 viên") được **nhân chuỗi** thành 180 viên;
  ô trống ở dòng sau của cùng đơn kế thừa dòng đầu; hai dòng giống hệt nhau của cùng đơn tính một lần; khách lẻ không phải khách;
  hai tên khách chỉ khác cách viết số điện thoại là một khách; sổ tay không có mã đơn mà dùng "STT" lặp trên các dòng của đơn
  thì STT là mã đơn; sổ mỗi dòng một phiếu (sửa xe) thì mỗi dòng là một đơn.
* Bảng tra tên → mã cho nhân viên và khách (`aliases`): "nhân viên Bùi Dũng Lan" lọc được đơn ghi `NV003`.
* **Tự kiểm tra**: `qty × đơn giá ≥ thành tiền` và tổng dòng bằng "tổng tiền hàng" của đơn. Tỉ lệ khớp trở thành đặc trưng
  tin cậy, quan trọng nhất với shop có định dạng lạ.
* Tương tự "evidence" của BIRD [R1], nhưng sinh tự động và kiểm chứng được. LLM thấy cả view lẫn bảng thô kèm lineage.

### 4.4 Rulebook theo split, dấu vân tay điều luật  (`rulebook.py`)
* Văn bản rulebook nằm ở đầu system prompt (giống nhau cho mọi câu nên dùng được prefix cache của vLLM [R32]).
* Các quy ước code cần (tuần bắt đầu thứ mấy, khung Tết, ca sáng/chiều, "tháng này" so với cùng số ngày, tiêu chí "bán chạy"…)
  được **đọc từ văn bản**; không tìm thấy mới dùng `configs/rulebook_overrides.yaml`.
* Mỗi template khai báo các điều luật nó cài đặt (`templates.NEEDS`). Điều luật không có trong rulebook của split thì template
  **tự tắt** và LLM trả lời từ văn bản. Kiểm chứng trên dữ liệu thật: rulebook public bỏ luật "giá nhập bình quân" nên template
  đó tắt ở public nhưng vẫn chạy ở train.

### 4.5 Thời gian và lịch âm  (`timeresolver.py`, `lunar.py`)
* LLM không tự tính ngày. Mọi cách nói được đổi thành khoảng `[start, end)` và đưa vào prompt dưới dạng literal SQL: hôm qua,
  N ngày gần nhất (kể cả hôm nay), tuần này/trước (thứ Hai–Chủ nhật), tháng này (đến hết hôm nay, so với cùng số ngày của tháng
  trước), tháng M không ghi năm, quý, "từ X đến Y", "từ 1/1 đến hết hôm nay", "xét các tháng A, B, C", "N ngày kể từ D so với N
  ngày liền trước", cùng kỳ năm trước, Tết Y = 23 tháng Chạp → mùng 10 (tra `calendar.csv`), "trước Tết", ca sáng/chiều,
  "đến hết ngày X" (snapshot).
* Lịch âm: ưu tiên file của BTC; fallback thuật toán Hồ Ngọc Đức [R37] (Meeus [R38]), đã kiểm thử Tết 2023–2027 và tháng nhuận 2025.

### 4.6 Hiểu câu hỏi  (`textnorm.py`, `question.py`, `linker.py`)
* Phân loại câu hỏi trước khi sinh như DIN-SQL [R3], nhưng bằng luật (không tốn lời gọi LLM): khoảng 30 ý định, kiểu đáp án,
  k, ngưỡng ("từ X trở lên", "≥ X", "trên X"), bộ lọc (nhóm hàng, nhóm khách, hình thức thanh toán, size).
* Liên kết giá trị: CHESS [R7] dùng LSH và CHASE-SQL [R8] dùng trích từ khoá kết hợp LSH. Dữ liệu mỗi shop nhỏ nên ta làm
  bản **chính xác**: so mọi span của câu hỏi với mọi giá trị phân biệt (rapidfuzz), chấm điểm cả trường hợp gọi tắt, và phát
  hiện nhập nhằng (AmbiQT [R25]). **Dấu thanh là bằng chứng**: câu có dấu "trả bằng chuyển khoản" không được liên kết với món
  "Trà"; câu không dấu thì giá trị một tiếng trùng từ vựng câu hỏi bị trừ điểm.
* Bộ lọc được nhận: mặt hàng, nhóm hàng, **thuộc tính danh mục** (`attributes.py`: hoạt chất, thương hiệu, danh mục… khớp
  đúng giá trị; cờ đúng/sai như "kê đơn" lấy nghĩa từ tên cột và câu README "`BanTheoDon` đúng là thuốc kê đơn", hiểu cả phủ
  định "không kê đơn", "trừ thuốc kê đơn ra"), nhân viên, khách, nhóm khách, hình thức thanh toán, kênh bán, ca, thứ trong tuần.
* `qnorm` chỉ sửa Telex khi kết quả là **một âm tiết tiếng Việt hợp lệ** (giữ "Acyclovir", "order"), và chỉ bung viết tắt
  nhập nhằng khi người hỏi gõ không dấu ("hong" → "không", nhưng "họng" giữ nguyên).

### 4.7 Template deterministic  (`templates.py`)
* Mỗi ý định của lõi bán hàng (và nhóm nhập hàng) là một chương trình nhỏ, kiểm toán được, có độ mạnh `strength`.
* Mỗi từ trong câu hỏi mà template không hiểu làm giảm độ mạnh (×0.6), nghĩa là template "biết mình không biết".
* **Ràng buộc chưa xử lý thì không trả lời**: nếu câu hỏi nêu đích danh một thực thể (mặt hàng, khách, nhân viên, nhà cung cấp…)
  hoặc một thuộc tính danh mục mà template không biến thành bộ lọc, template tự rút lui thay vì trả lời câu hỏi rộng hơn
  (đây là lỗi phổ biến nhất khi đo trên train: trả doanh thu cả shop cho câu hỏi về một hoạt chất).
* Template là **một họ ứng viên độc lập**: khi khớp với LLM thì là tín hiệu tin cậy mạnh (đồng thuận giữa các họ).

### 4.8 LLM: sinh đa dạng, thực thi, tự sửa, bỏ phiếu  (`agent.py`, `prompts.py`, `executor.py`)

| Họ ứng viên | Nguồn |
|---|---|
| SQL trực tiếp × n (batch, seed cố định) | self-consistency [R11]; voting trong Arctic-Text2SQL-R1 [R10] |
| Plan → SQL | query-plan CoT / divide-and-conquer của CHASE-SQL [R8] |
| pandas (CodeAct) | CodeAct [R13]; InfiAgent-DABench [R15] |
| ReAct thăm dò dữ liệu | ReAct [R12]; bối cảnh agentic của Spider 2.0 [R2] |
| Tự sửa bằng phản hồi thực thi | execution-guided decoding [R16]; Self-Debugging [R17]; query fixer của CHASE-SQL [R8] |
| Gom cụm theo đáp án đã thực thi rồi bỏ phiếu | semantic entropy [R23, R24]; voting là baseline abstention mạnh trong TrustSQL [R19] |
| Judge một lần khi hai cụm sát nhau | bản rút gọn của selector pairwise trong CHASE-SQL [R8] (bản gốc tốn hàng chục lời gọi/câu) |

* Không dùng MCTS (Alpha-SQL [R4]) vì quá tốn thời gian so với tie-break theo thời gian inference.
* Bài học từ smoke test với model nhỏ: khi được yêu cầu sửa lỗi, model hay "đầu hàng" bằng `unanswerable`. Vì vậy
  `unanswerable` phát ra ở bước sửa lỗi bị coi là thất bại, không phải một phiếu.

### 4.9 Khả năng trả lời  (`answerability.py`)
* Chỉ trả `unanswerable` khi có **bằng chứng dương** (PRACTIQ [R22] cùng rulebook §7): kỳ hoàn toàn ngoài dữ liệu (kỳ chỉ
  giao một phần thì vẫn tính), shop không ghi loại thông tin cần (thanh toán, kênh, nhóm khách, địa chỉ, giới tính, lý do,
  tồn kho, nhập hàng, giá vốn, công nợ, hạn dùng, giờ, nhân viên), câu chủ quan, mặt hàng không tồn tại.

### 4.10 Calibration và ngưỡng  (`calibrate.py`, `scripts/fit_calibrator.py`)
* Đặc trưng: mức đồng thuận, khoảng cách giữa hai cụm đầu, đồng thuận giữa các họ, template có khớp không, độ mạnh template,
  tỉ lệ lỗi/rỗng (bộ lọc mạnh nhất ở EHRSQL-2024: PLUQ [R20], ProbGate [R21]), bằng chứng unanswerable, nhập nhằng liên kết,
  tự kiểm tra của view, ý định (one-hot).
* Logistic (cộng isotonic khi có ≥300 mẫu), **CV nhóm theo shop**. Không chọn ngưỡng theo leaderboard (bài học từ EHRSQL-2024 [R18]).
* Conformal abstention [R26] chỉ dùng để kiểm tra an toàn, vì PA tối đa hoá 2p − 1 chứ không ràng buộc tỉ lệ lỗi.

### 4.11 Mô hình và vận hành theo luật thi
* **≤ 15B tham số**: Qwen3-14B [R35] (14,8B, Apache-2.0, phát hành 04/2025) chiếm gần trọn ngân sách nên mặc định không có
  model thứ hai. `run.py` kiểm tra tổng tham số đã khai báo.
* Serving: vLLM [R32] TP=2 trên 2×RTX 5090, bật prefix caching (rulebook và thẻ shop dùng chung). Chế độ thinking của Qwen3 bật
  mặc định (6 giờ cho khoảng 500 câu là khoảng 40 giây mỗi câu).
* **Tái lập**: seed mỗi lời gọi suy từ nội dung câu hỏi (không từ id); `temperature` cố định; xử lý tuần tự từng shop.
* **Ngân sách 6 giờ**: ghi `submission.jsonl` theo từng dòng; sau `fast_after_s` chỉ dùng template và luật; quá hạn thì abstain.
* **Log**: `/result/logs/<id>.json` gồm đáp án, thời gian, ý định, khoảng thời gian, mọi chương trình ứng viên và output.

## 5. Kết quả

Chi tiết ở [`RESULTS.md`](RESULTS.md). Tóm tắt chế độ không LLM: train PA 0,888 theo luật chấm chính thức (2.667 đúng,
3 sai, 330 bỏ qua; số trong mẫu vì lỗi được tìm trên train), public trả lời 635/1.000 câu (24 `unanswerable`) trong khoảng
7 phút trên CPU.

## 6. Chiến lược tổng quát hoá sang business type mới (private test)
1. Không có logic theo id câu hỏi hay id shop (luật thi số 4); không tra đáp án.
2. Vai trò cột lấy từ README trước, tên cột (Việt/Anh) sau, kèm kiểm tra kiểu giá trị.
3. View tự kiểm tra; khi lệch thì tin cậy giảm và LLM được thấy cảnh báo.
4. Dấu vân tay điều luật: luật mới hoặc thay đổi thì template tắt, LLM đọc văn bản.
5. Calibrator được đánh giá bằng CV nhóm theo shop, không theo câu.
6. Ý định đặc thù ngành (sửa xe, điện thoại, mẹ & bé, thuốc…) để LLM xử lý trên view và bảng thô.

## 7. Kế hoạch thí nghiệm
| # | Cấu hình | Mục đích |
|---|---|---|
| A0 | Template + luật (không LLM) | baseline an toàn; dùng khi hết giờ |
| A1 | + SQL × 3, không template | đóng góp riêng của LLM |
| A2 | + template (đầy đủ vòng 1) | đồng thuận giữa các họ |
| A3 | + vòng 2 (plan, pandas, ReAct) | cứu câu khó |
| A4 | thinking on/off | trade-off PA và thời gian |
| A5 | calibrator fit (CV theo shop) với τ = 0.5 hoặc τ tối ưu | PA cuối |
Cách chạy: mọi biến thể là cờ `--set` của `scripts/run.py` (ví dụ `--set agent.use_templates=false`).

## 8. Giới hạn
* Chưa chạy được LLM thật trên GPU trong môi trường phát triển (chỉ có CPU 2 nhân). Đường LLM đã được kiểm thử bằng LLM giả lập
  và một model 1,5B trên CPU. Calibrator cho chế độ có LLM **phải được fit lại** bằng một lần chạy trên train với GPU.
* Các ý định ngoài lõi bán hàng (công nợ, tồn kho, lô/hạn dùng, giao hàng, ngành đặc thù) hoàn toàn dựa vào LLM.
