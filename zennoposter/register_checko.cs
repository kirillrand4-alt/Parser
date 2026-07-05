// =============================================================================
//  ZennoPoster: регистратор аккаунтов на checko.ru + подтверждение по email
// =============================================================================
//  Назначение:  разовая регистрация СВОИХ аккаунтов на checko.ru из CSV,
//               с автоматическим подтверждением почты через IMAP (Gmail).
//
//  Куда вставлять: кубик "Свой код" (CodeCreator) в проекте ZennoPoster.
//                  Тип действия — C# code.
//
//  ВАЖНО про почту:
//    Письмо-подтверждение читается по IMAP (imap.gmail.com:993) с помощью
//    "Пароля приложения" Google — это официальный способ.
//    Веб-вход в Gmail НЕ автоматизируется (Google это блокирует).
//    Как получить пароль приложения:
//      Аккаунт Google -> Безопасность -> включить 2FA
//                     -> "Пароли приложений" -> создать -> 16 символов.
//    Этот пароль кладём в CSV-колонку email_app_password (или в imapAppPassword).
//
//  Как настроить под форму checko.ru:
//    Запустите проект в записи, откройте форму регистрации,
//    ПКМ по полю -> "Показать в HTML" -> скопируйте XPath в блок SETTINGS.
// =============================================================================

// ------------------------------- SETTINGS ------------------------------------
string csvPath     = @"C:\Zenno\checko\accounts.csv";        // входной файл
string resultPath  = @"C:\Zenno\checko\accounts_result.csv"; // отчёт
string regUrl      = @"https://checko.ru/";                  // страница/модалка регистрации

// --- Прокси (по одному на аккаунт, по кругу) ---
// Файл: по одной прокси в строке. Форматы:
//   ip:port            |  login:pass@ip:port  |  http://login:pass@ip:port
//   socks5://ip:port   |  socks5://login:pass@ip:port
// Пустая строка "" в proxyListPath = работать без прокси.
string proxyListPath = @"C:\Zenno\checko\proxies.txt";
bool   proxyRequired = true;   // true: без живой прокси аккаунт пропускается; false: работаем напрямую
int    proxyMaxTry   = 3;      // сколько прокси перебрать, если предыдущая не отвечает

// --- XPath полей формы регистрации (подставьте под checko.ru) ---
// На checko.ru обычно нужны только email и пароль — лишние поля оставьте "".
string xpOpenSignup = "//a[contains(.,'Регистрация')] | //button[contains(.,'Регистрация')]"; // открыть форму (если модалка); "" если форма сразу на странице
string xpEmail      = "//input[@type='email']";
string xpPassword   = "//input[@type='password']";
string xpPassword2  = "";                                    // повтор пароля, если есть
string xpAgree      = "//input[@type='checkbox']";           // согласие с условиями; "" если нет
string xpSubmit     = "//button[@type='submit']";            // кнопка отправки

// Признак, что форма отправлена и письмо ушло (по желанию):
string sentText     = "письмо";                              // подстрока на странице после отправки (нижн. регистр); "" пропустить

// --- IMAP (Gmail) для чтения письма-подтверждения ---
string imapHost           = "imap.gmail.com";
int    imapPort           = 993;
string imapAppPasswordDef = "";                              // пароль приложения по умолчанию (если не задан в CSV)
string mailFromFilter     = "checko";                        // фильтр отправителя (SEARCH FROM)
string confirmLinkRegex   = @"https?://[^\s""'<>]*checko\.ru[^\s""'<>]*(?:confirm|activ|verif|token|key)[^\s""'<>]*";
int    mailWaitSeconds    = 120;                             // сколько ждать письмо
int    mailPollSeconds    = 8;                               // интервал опроса ящика

// --- Признак успешно подтверждённого аккаунта (после перехода по ссылке) ---
string xpSuccessMark = "";                                   // напр. //*[contains(.,'подтвержд')]
string successText   = "подтвержд";                          // подстрока на странице; "" пропустить

bool typingEmulation = true;
int  minDelayMs = 800, maxDelayMs = 2200;
// -----------------------------------------------------------------------------

var rnd = new Random();
Action pause = () => System.Threading.Thread.Sleep(rnd.Next(minDelayMs, maxDelayMs));

Action<string,string,string> fill = (xpath, value, label) => {
    if (string.IsNullOrEmpty(xpath) || value == null) return;
    var el = instance.ActiveTab.FindElementByXPath(xpath, 0);
    if (el.IsVoid) { project.SendWarningToLog("Поле не найдено: " + label, true); return; }
    el.SetValue(value, typingEmulation ? "Full" : "None", typingEmulation);
    pause();
};

// ---- IMAP: минимальный клиент, ищет свежее письмо и достаёт ссылку ----
Func<string,string,string,string> getConfirmLink = (imapUser, imapPass, fromFilter) => {
    var tcp = new System.Net.Sockets.TcpClient();
    tcp.Connect(imapHost, imapPort);
    var ssl = new System.Net.Security.SslStream(tcp.GetStream());
    ssl.AuthenticateAsClient(imapHost);

    int tag = 0;
    Func<string,string> cmd = (line) => {
        string t = "a" + (++tag) + " ";
        var data = System.Text.Encoding.ASCII.GetBytes(t + line + "\r\n");
        ssl.Write(data);
        var sb = new System.Text.StringBuilder();
        var buf = new byte[8192];
        // читаем до завершающей строки с нашим тегом (aN OK/NO/BAD)
        while (true) {
            int n = ssl.Read(buf, 0, buf.Length);
            if (n <= 0) break;
            sb.Append(System.Text.Encoding.UTF8.GetString(buf, 0, n));
            string s = sb.ToString();
            if (s.Contains("\r\n" + t.Trim() + " OK") || s.Contains("\r\n" + t.Trim() + " NO")
                || s.Contains("\r\n" + t.Trim() + " BAD") || s.StartsWith(t.Trim() + " ")) break;
        }
        return sb.ToString();
    };

    try {
        var greet = new byte[4096]; ssl.Read(greet, 0, greet.Length);      // приветствие сервера
        var login = cmd("LOGIN \"" + imapUser + "\" \"" + imapPass + "\"");
        if (!login.Contains(" OK")) { ssl.Dispose(); tcp.Close();
            project.SendWarningToLog("IMAP LOGIN не удался для " + imapUser, true); return null; }
        cmd("SELECT INBOX");

        var search = cmd("SEARCH FROM \"" + fromFilter + "\"");
        var ids = System.Text.RegularExpressions.Regex.Matches(search, @"\d+");
        if (ids.Count == 0) { ssl.Dispose(); tcp.Close(); return null; }
        string lastId = ids[ids.Count - 1].Value;               // самое свежее письмо

        var body = cmd("FETCH " + lastId + " (BODY[TEXT])");
        ssl.Dispose(); tcp.Close();

        // грубое декодирование quoted-printable (ссылки в HTML-письмах часто закодированы)
        body = body.Replace("=\r\n", "");
        body = System.Text.RegularExpressions.Regex.Replace(body, "=([0-9A-Fa-f]{2})",
            m => ((char)Convert.ToInt32(m.Groups[1].Value, 16)).ToString());

        var link = System.Text.RegularExpressions.Regex.Match(body, confirmLinkRegex);
        return link.Success ? link.Value : null;
    } catch (Exception ex) {
        project.SendWarningToLog("IMAP ошибка: " + ex.Message, true);
        try { ssl.Dispose(); tcp.Close(); } catch {}
        return null;
    }
};

// --------------------------- Загрузка прокси ---------------------------
var proxies = new System.Collections.Generic.List<string>();
if (!string.IsNullOrEmpty(proxyListPath) && System.IO.File.Exists(proxyListPath))
    foreach (var p in System.IO.File.ReadAllLines(proxyListPath))
        if (!string.IsNullOrWhiteSpace(p) && !p.TrimStart().StartsWith("#"))
            proxies.Add(p.Trim());
project.SendInfoToLog("Прокси в списке: " + proxies.Count, true);
int proxyIdx = 0;   // указатель round-robin (общий на весь прогон)

// Ставит следующую по кругу рабочую прокси. Возвращает применённую строку или null.
Func<string> applyNextProxy = () => {
    if (proxies.Count == 0) { instance.SetProxy(""); return proxyRequired ? null : ""; }
    for (int t = 0; t < Math.Min(proxyMaxTry, proxies.Count); t++) {
        string prx = proxies[proxyIdx % proxies.Count];
        proxyIdx++;
        instance.SetProxy(prx);
        try {
            // проверка живости: тянем свой IP через прокси
            string ip = instance.ActiveTab.HttpGet("https://api.ipify.org", "", "", 15000);
            if (!string.IsNullOrEmpty(ip)) {
                project.SendInfoToLog("Прокси OK: " + prx + " -> " + ip.Trim(), false);
                return prx;
            }
        } catch (Exception ex) {
            project.SendWarningToLog("Прокси не отвечает: " + prx + " (" + ex.Message + ")", false);
        }
    }
    return null;   // ни одна из перебранных не ответила
};

// --------------------------- Чтение CSV ---------------------------
if (!System.IO.File.Exists(csvPath)) throw new Exception("Нет файла: " + csvPath);
var lines = System.IO.File.ReadAllLines(csvPath, System.Text.Encoding.UTF8);
if (lines.Length < 2) throw new Exception("В CSV нет данных.");

var header = lines[0].Split(';');
Func<string[],string,string> col = (row, name) => {
    for (int i = 0; i < header.Length; i++)
        if (header[i].Trim().Equals(name, StringComparison.OrdinalIgnoreCase))
            return i < row.Length ? row[i].Trim() : "";
    return "";
};

var report = new System.Text.StringBuilder();
report.AppendLine("email;status;detail");
int ok = 0, fail = 0;

for (int r = 1; r < lines.Length; r++)
{
    if (string.IsNullOrWhiteSpace(lines[r])) continue;
    var row = lines[r].Split(';');
    string email = col(row, "email");
    string pass  = col(row, "password");
    string appPw = col(row, "email_app_password");
    if (string.IsNullOrEmpty(appPw)) appPw = imapAppPasswordDef;

    project.SendInfoToLog("=== Регистрация: " + email + " ===", true);
    try
    {
        // назначаем прокси на этот аккаунт
        string usedProxy = applyNextProxy();
        if (usedProxy == null && proxyRequired) {
            fail++; report.AppendLine(email + ";SKIP;нет живой прокси");
            project.SendWarningToLog("Пропуск " + email + ": нет живой прокси", true);
            continue;
        }

        instance.ActiveTab.Navigate(regUrl, "");
        instance.ActiveTab.WaitDownloading();
        pause();

        if (!string.IsNullOrEmpty(xpOpenSignup)) {
            var open = instance.ActiveTab.FindElementByXPath(xpOpenSignup, 0);
            if (!open.IsVoid) { open.Click(); pause(); }
        }

        fill(xpEmail,     email, "email");
        fill(xpPassword,  pass,  "password");
        fill(xpPassword2, pass,  "password2");

        if (!string.IsNullOrEmpty(xpAgree)) {
            var chk = instance.ActiveTab.FindElementByXPath(xpAgree, 0);
            if (!chk.IsVoid && chk.GetAttribute("checked") != "true") { chk.Click(); pause(); }
        }

        var btn = instance.ActiveTab.FindElementByXPath(xpSubmit, 0);
        if (btn.IsVoid) throw new Exception("Кнопка отправки не найдена: " + xpSubmit);
        btn.Click();
        instance.ActiveTab.WaitDownloading();
        pause();

        if (!string.IsNullOrEmpty(sentText) &&
            !instance.ActiveTab.DocumentText.ToLower().Contains(sentText.ToLower()))
            project.SendWarningToLog("Не видно подтверждения отправки формы для " + email, true);

        // --- ждём письмо и переходим по ссылке подтверждения ---
        string link = null;
        int waited = 0;
        while (waited < mailWaitSeconds) {
            System.Threading.Thread.Sleep(mailPollSeconds * 1000);
            waited += mailPollSeconds;
            link = getConfirmLink(email, appPw, mailFromFilter);
            if (link != null) break;
            project.SendInfoToLog("Жду письмо... " + waited + "s", false);
        }

        if (link == null) {
            fail++; report.AppendLine(email + ";FAIL;письмо подтверждения не пришло за " + mailWaitSeconds + "s");
            project.SendWarningToLog("Нет письма для " + email, true); pause(); continue;
        }

        project.SendInfoToLog("Ссылка подтверждения: " + link, true);
        instance.ActiveTab.Navigate(link, "");
        instance.ActiveTab.WaitDownloading();
        pause();

        bool success = true;
        if (!string.IsNullOrEmpty(xpSuccessMark))
            success = !instance.ActiveTab.FindElementByXPath(xpSuccessMark, 0).IsVoid;
        if (success && !string.IsNullOrEmpty(successText))
            success = instance.ActiveTab.DocumentText.ToLower().Contains(successText.ToLower());

        if (success) { ok++; report.AppendLine(email + ";OK;подтверждён");
            project.SendInfoToLog("OK: " + email, true); }
        else { fail++; report.AppendLine(email + ";PARTIAL;форма отправлена, подтверждение не проверено");
            project.SendWarningToLog("Подтверждение не проверено: " + email, true); }
    }
    catch (Exception ex) {
        fail++; report.AppendLine(email + ";ERROR;" + ex.Message.Replace(";", ","));
        project.SendErrorToLog("Ошибка на " + email + ": " + ex.Message, true);
    }
    pause();
}

System.IO.File.WriteAllText(resultPath, report.ToString(), System.Text.Encoding.UTF8);
project.SendInfoToLog("Готово. OK: " + ok + ", ошибок: " + fail + ". Отчёт: " + resultPath, true);
