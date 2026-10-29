# 共用 Codex JSON 呼叫模組

本模組使用 Python → 已登入的 Codex CLI → LLM → JSON，供未來各步驟共用。本次沒有建立任何 Pipeline。核心實作是 [codex_client.py](../src/llm/codex_client.py)，公開匯出位於 [llm 套件](../src/llm/__init__.py)。

## 環境與依賴

開發時以本機 `codex.cmd --version`、`codex.cmd exec --help` 和 `codex.cmd --help` 核對，版本為 **0.165.0**。Python 為 **3.12.16**。沒有重新安裝 Python、Node、Codex 或 uv，也沒有修改既有虛擬環境的內容。

[requirements.txt](../requirements.txt) 固定此次驗證的六個套件版本：

| 套件 | 版本 | 用途 |
| --- | --- | --- |
| jsonschema | 4.26.0 | Schema 本身與回傳資料驗證 |
| referencing | 0.37.0 | 控制 Schema 參照解析，拒絕自動讀取外部資源 |
| attrs | 26.1.0 | 相依套件 |
| jsonschema-specifications | 2025.9.1 | 標準 Schema 資源 |
| rpds-py | 2026.9.1 | 相依套件 |
| typing-extensions | 4.16.0 | 相依套件 |

本次測試以 `uv pip install --target` 將套件安裝在系統暫存區，沒有安裝進專案原有虛擬環境。日後建議使用以下 `uv run --isolated` 指令，由 uv 建立快取中的獨立執行環境；原有虛擬環境僅提供 Python 解譯器。第一次可能需要下載上述套件，不會下載新的 Python。

## 離線測試

在任意 PowerShell 工作目錄執行：

```powershell
uv run --directory 'C:\Users\User\Desktop\知識圖譜' --no-config --no-project --no-env-file --isolated --no-python-downloads --python '.\.venv\Scripts\python.exe' --with-requirements requirements.txt python -B -m unittest discover -s tests -v
```

依賴已快取後可加入 `--offline`，禁止 uv 下載。測試內容完全 Mock `subprocess.Popen` 和 `subprocess.run`，不會啟動 Codex 或 taskkill，不會呼叫 LLM。使用標準函式庫 `unittest`，沒有 pytest。

## 公開介面

```python
CodexClient(
    *,
    codex_command=None,       # Windows 預設在 PATH 尋找 codex.cmd
    temp_dir=None,            # 暫存資料夾的既有父目錄；預設使用系統暫存區
    max_output_bytes=1_048_576,
)

client.generate_json(
    prompt,
    schema,                  # Python dict；從檔案讀取時由呼叫端 json.loads
    *,
    timeout=120.0,           # 每次嘗試的秒數
    max_retries=0,           # 額外嘗試次數，預設零
    retry_on=(ErrorKind.TIMEOUT,),
    retry_delay=0.0,         # 兩次嘗試間固定等待秒數
    model=None,              # 未指定時採 CLI 的預設模型
) -> GenerationResult
```

`CodexClient` 建構與匯入不會呼叫 CLI；僅明確呼叫 `generate_json()` 才會執行。`codex_command` 是可信任的執行檔名稱或完整路徑，不接受含額外參數的命令字串。每次呼叫各自保存結果，沒有跨呼叫累加的可變狀態。

基本用法（此程式碼會進行真實呼叫，請自行決定執行時機）：

```python
import json
from pathlib import Path
from src.llm import CodexClient

schema = json.loads(
    Path("schemas/codex_smoke.schema.json").read_text(encoding="utf-8")
)
result = CodexClient().generate_json(
    '只回傳 {"success": true}，不要使用工具或讀取檔案。',
    schema,
    timeout=120,
    max_retries=0,
)
if result.success:
    print(result.data)
else:
    print(result.error.kind, result.error.message)
print(result.attempt_count, result.retry_count)
```

請先判斷 `result.success`，再使用 `result.data`。成功回傳 JSON `null` 時資料也會是 `None`，所以不能只靠資料是否為 `None` 判斷成功。支援物件、陣列與其他符合 Schema 的 JSON 根值；CLI 端另有結構化輸出的 Schema 限制。

### 結果與執行紀錄

| 欄位 | 意義 |
| --- | --- |
| `success` | CLI 正常退出、JSON 解析、Schema 驗證及暫存清理全部成功 |
| `data` | 驗證通過的 Python JSON 值；失敗固定為 `None` |
| `error` | 失敗時的 `GenerationError(kind, message)`；成功為 `None` |
| `attempt_count` | 此次呼叫嘗試啟動 CLI 的次數，包括啟動失敗 |
| `retry_count` | `max(0, attempt_count - 1)` |
| `attempts` | `AttemptRecord` 的 tuple，依嘗試順序保存 |

每筆 `AttemptRecord` 包含 `number`、`elapsed_seconds`、`returncode`、`error` 和 `success` 屬性。耗時包含該次暫存檔處理與清理；無法取得退出碼時為 `None`。Schema 前置檢查失敗或 PATH 找不到 CLI 時，次數為零；開始準備單次呼叫後遇到本機 I/O 或啟動失敗仍記一次嘗試。

可使用 `dataclasses.asdict(result)` 轉為可序列化資料，並透過 `json.dumps()` 輸出。模組只使用自己的 Python logger，不設定全域 logging、不建立日誌檔。呼叫端若需要日誌，可自行設定 `logging.basicConfig(level=logging.INFO)`。日誌包含次數、狀態、退出碼與耗時，不包含 Prompt、Schema、答案、環境變數、原始 stdout/stderr 或暫存路徑。失敗結果不保存原始模型答案；成功資料則是呼叫端要求的 JSON，呼叫端應依自身資料敏感程度決定是否另外保存。

## Timeout 與 Retry

`timeout` 必須為大於零的有限秒數。它控制單次 `Popen.communicate()`；不包含依賴下載、程序建立前的本機操作及終止後清理時間，也不是整個函式的總時間上限。Windows 逾時後執行 `taskkill.exe /PID <pid> /T /F` 終止此次 CLI 的程序樹，再回收主程序；清理動作各有五秒上限。使用 `--no-daemon`，避免把模型執行交給共用背景伺服器。若無法確認程序樹已終止，回傳 `process_cleanup` 並禁止重試。非 Windows 分支使用獨立 process group；本次只驗證 Windows 環境。

預設 `max_retries=0`，所以至多嘗試一次。只調高 `max_retries` 時，僅重試逾時。JSON 與 Schema 不符合必須額外指定才會重試，例如：

```python
from src.llm import ErrorKind

result = client.generate_json(
    prompt,
    schema,
    max_retries=1,
    retry_on=(ErrorKind.TIMEOUT, ErrorKind.INVALID_JSON, ErrorKind.SCHEMA_VALIDATION),
    retry_delay=1.0,
)
```

這會最多啟動兩次 CLI。`retry_on=()` 可停用所有重試。重試使用相同 Prompt 與 Schema，每次建立新的會話與暫存目錄，不延續失敗會話、不加入原始錯誤答案。

CLI 非零退出可能是登入、額度、設定或網路問題；因退出碼無法可靠辨識，模組一律不重試。也不能把 `CLI_EXECUTION`、`IO_ERROR` 等永久錯誤加入 `retry_on`，否則立即拋出 `ValueError`。每次重試都可能額外消耗使用額度，逾時前服務端也可能已完成請求。

## Schema 與 JSON

呼叫前以 `jsonschema` 檢查 Schema 本身，再將快照寫入 UTF-8 暫存檔並傳給 `--output-schema`。未指定 `$schema` 時使用 Draft 2020-12；明確指定未知方言會在呼叫前拒絕。JSON Schema 的 `format` 在此作為註記，不額外啟用格式檢查器。可使用 `pattern` 等約束，或由後續業務邏輯驗證格式。

CLI 成功後，只讀取 `--output-last-message` 指定的檔案，再做嚴格 UTF-8 JSON 解析和本機 Schema 驗證。空字串、Markdown code fence、附加說明、多份 JSON、重複物件鍵、NaN、Infinity、非有限浮點數或 UTF-8 BOM 均視為 `invalid_json`，不自動猜測、擷取或修補。預設答案上限為 1 MiB，可由 `max_output_bytes` 調整。

支援同份 Schema 內的 `$defs`／`$ref`。本機驗證器不自動下載 HTTP Schema，也不讀取 `file://` 外部資源。無法解析的參照回傳 `invalid_schema`，部分參照問題會在 CLI 回傳後才被發現，因此可能已產生一次呼叫。建議使用完整、自足的 Schema。CLI／服務端只接受 JSON Schema 的子集合；本機驗證通過不代表所有方言或關鍵字都能被服務端接受。範例 [codex_smoke.schema.json](../schemas/codex_smoke.schema.json) 使用明確的 `required` 與 `additionalProperties: false`。

## 錯誤分類

| `ErrorKind` | 回傳字串 | 處理方式 |
| --- | --- | --- |
| `INVALID_SCHEMA` | `invalid_schema` | 修正 Schema 或參照；不重試 |
| `CLI_NOT_FOUND` | `cli_not_found` | 檢查 PATH 或明確指定 CLI 路徑；不重試 |
| `CLI_EXECUTION` | `cli_execution` | 查看安全保存的退出碼；不重試 |
| `TIMEOUT` | `timeout` | 預設不重試；明確提高次數才啟用 |
| `INVALID_JSON` | `invalid_json` | 缺少答案或解析失敗；僅明確選擇時可重試 |
| `SCHEMA_VALIDATION` | `schema_validation` | 答案不符合 Schema；僅明確選擇時可重試 |
| `IO_ERROR` | `io_error` | 暫存目錄、權限、程序啟動或清理失敗；不重試 |
| `OUTPUT_LIMIT` | `output_limit` | 縮小輸出或調整限制；不重試 |
| `PROCESS_CLEANUP` | `process_cleanup` | 不能確認程序樹終止；停止並由使用者檢查 |

無效的 Prompt、Timeout、Retry 或 model 參數會拋出 `ValueError`，不啟動 CLI。`KeyboardInterrupt` 等中斷會嘗試終止程序並清理暫存檔後重新拋出。

## Windows 與暫存檔策略

實際命令的固定結構為：

```text
cmd.exe /d /s /v:off /c ""<codex.cmd>" "--no-daemon" "--ask-for-approval" "never" "exec" "--ignore-user-config" "--ignore-rules" "--sandbox" "read-only" "--skip-git-repo-check" "--ephemeral" "--color" "never" "--output-schema" "schema.json" "--output-last-message" "response.json" "-""
```

使用 `shell=False` 明確啟動 Windows 的 `cmd.exe`，逐項加引號，停用 AutoRun 與 delayed expansion；不透過 PowerShell、不使用 ps1。Prompt 只經 UTF-8 stdin 傳入，中文、換行、引號、`&`、`%` 等不進入命令列。Schema 與答案使用固定相對檔名，暫存目錄以 `cwd` 傳入而不拼接到 shell。

CLI 執行檔路徑允許空白、中文與加引號後的 `&`，但含 `%`、`!`、雙引號、NUL 或換行的執行檔路徑會被拒絕。`model` 只接受識別字元。此設計不接受任意附加 CLI 參數。

每次嘗試在系統暫存區建立 `kg-codex-*` 資料夾，只有輸入 Schema 與最後 JSON 訊息。正常結束、解析失敗、逾時及可處理的例外都會經過 `TemporaryDirectory` 清理；清理失敗會回報錯誤。若主機斷電、Python 被強制終止或檔案被其他程序鎖定，無法保證自動清除，需由使用者處理。

CLI 的工作目錄是這個暫存目錄，不是專案根目錄。使用 read-only sandbox、禁止互動核准、忽略使用者 config 與 execpolicy rules，不載入使用者設定中的模型選擇、MCP 或 hook 設定；登入仍使用 CLI 既有的登入位置。必要時請透過 `model=` 明確指定模型。模組不讀取、複製、修改登入憑證，並從子程序環境移除 `OPENAI_API_KEY` 與 `CODEX_API_KEY`，使用既有 ChatGPT 登入。其他 CLI 本身的全域行為仍由版本決定，暫存 cwd 與 read-only sandbox 不代表作業系統層級的機密檔隔離。

原始 stdout/stderr 直接導向 DEVNULL，不回傳 JSONL、推理紀錄或完整診斷訊息；只從最後訊息檔取回結果。這犧牲部分 CLI 故障診斷細節。認證問題請另行檢查 CLI 登入狀態，勿把憑證貼到日誌。`--ephemeral` 表示不保存會話 rollout，並不保證 CLI 完全不更新自己的快取或一般執行資訊。

## 真實連線驗收（由使用者執行）

本次開發只完成離線 Mock 測試，沒有執行以下指令。它使用正式 [try_codex_client.py](../scripts/try_codex_client.py) 與 [Schema](../schemas/codex_smoke.schema.json)，發起一次小型真實 JSON 呼叫，預設零重試：

```powershell
uv run --directory 'C:\Users\User\Desktop\知識圖譜' --no-config --no-project --no-env-file --isolated --no-python-downloads --python '.\.venv\Scripts\python.exe' --with-requirements requirements.txt python -B -m scripts.try_codex_client
```

成功時程序退出碼為 `0`，輸出包括：

```json
{
  "success": true,
  "schema_valid": true,
  "attempt_count": 1,
  "retry_count": 0,
  "error": null
}
```

另有 `attempts` 陣列供查看退出碼及耗時。失敗退出碼為 `1`，同時回報經過內容限制的錯誤。此處的次數只代表 Python 啟動 CLI 的嘗試數，不是 CLI 內部 HTTP 重試數、token 數、帳單請求數或精確額度用量；模組沒有改寫 CLI 內部傳輸重試設定。

Windows 程序參數與清理流程均已 Mock 驗證，真實登入、服務端 Schema 相容性、額度與實際逾時終止效果仍待使用者真實驗收。CLI 更新後應重新核對 `--help`，不應假設不同版本相容。

## 參考資料

實際使用的參數以本機 CLI 0.165.0 的 help 為準；補充核對 [OpenAI 官方 Non-interactive mode 文件](https://learn.chatgpt.com/docs/non-interactive-mode)，其中區分 stdout JSONL 事件、最後訊息檔與 `--output-schema`。本模組不使用 `--json`，以免將事件流誤當答案。
