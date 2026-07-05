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
string regUrl      = @"https://checko.ru/sign-up";           // страница регистрации

// --- Прокси ---
// Приоритет: CSV-колонка "proxy" (липкая на аккаунт) > список ниже (round-robin).
// Файл: по одной прокси в строке. Форматы:
//   ip:port            |  login:pass@ip:port  |  http://login:pass@ip:port
//   socks5://ip:port   |  socks5://login:pass@ip:port
// Пустая строка "" в proxyListPath = работать без прокси.
string proxyListPath = @"C:\Zenno\checko\proxies.txt";
bool   proxyRequired = true;   // true: без живой прокси аккаунт пропускается; false: работаем напрямую
int    proxyMaxTry   = 3;      // сколько прокси перебрать, если предыдущая не отвечает

// --- XPath полей формы регистрации checko.ru /sign-up (по реальному HTML) ---
string xpOpenSignup = "";                                             // форма сразу на странице
string xpEmail      = "//input[@name='user[email]']";
string xpPassword   = "//input[@name='user[password]']";
string xpPassword2  = "//input[@name='user[password_confirmation]']";
string xpAgree      = "//input[@id='personal_information']";          // чекбокс согласия
string xpSubmit     = "//button[contains(@class,'btn-primary') and contains(.,'Зарегистрироваться')]";

// Признак, что форма отправлена (по желанию). Оставлено "" — checko после
// отправки редиректит; факт успеха проверяем уже по письму/ссылке.
string sentText     = "";                                    // подстрока на странице после отправки; "" пропустить

// --- Способ чтения письма-подтверждения ---
//   "imap"      — по IMAP с паролём приложения (надёжно, без входа в браузер).
//   "gmail_web" — открыть Gmail в ЗАРАНЕЕ авторизованном профиле и прочитать письмо.
//                 Профиль готовится один раз вручную: prepare_google_profile.cs.
string emailReadMode      = "imap";

// --- Пароль для аккаунта на checko ---
//   true  — пароль на checko = ПАРОЛЬ ОТ ПОЧТЫ (CSV-колонка email_password).
//           ВНИМАНИЕ: повторное использование пароля почты небезопасно —
//           при утечке checko раскроется и доступ к почте. Осознанный выбор.
//   false — берётся отдельный пароль из CSV-колонки password.
bool   siteSameAsEmailPassword = true;

// --- Профиль браузера (нужен для режима gmail_web) ---
//   Путь берётся из CSV-колонки "profile"; загружается перед работой с аккаунтом.
//   (Вход через Google на самом checko отключён сервисом, для регистрации не нужен.)
bool   loadProfile        = true;   // false = профиль не грузим
// Для аккаунтов БЕЗ сохранённого профиля (обычно режим imap): генерировать
// свежий профиль ZP на каждый аккаунт — это даёт консистентный User-Agent и
// отпечаток (Canvas/WebGL/шрифты) под каждую учётку. Сохранённые Google-профили
// не трогаются (чтобы не рвать сессию Gmail).
bool   regenProfilePerAccount = true;

// XPath для режима gmail_web (Gmail в браузере). При смене вёрстки подправьте.
string gmailSearchUrl     = @"https://mail.google.com/mail/u/0/#search/from%3Achecko+newer_than%3A1d";
string xpGmailFirstMail   = "//tr[contains(@class,'zA')][1]"; // первая строка списка писем
// После открытия письма ссылка ищется тем же confirmLinkRegex по тексту страницы.

// --- IMAP (Gmail) для чтения письма-подтверждения ---
string imapHost           = "imap.gmail.com";
int    imapPort           = 993;
string imapAppPasswordDef = "";                              // пароль приложения по умолчанию (если не задан в CSV)
string mailFromFilter     = "checko";                        // отправитель no-reply@checko.ru
// Точная ссылка активации: https://checko.ru/user/email/confirm/<uuid>
string confirmLinkRegex   = @"https?://checko\.ru/user/email/confirm/[0-9a-fA-F-]{36}";
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

// ---- Gmail в браузере (профиль уже авторизован через prepare_google_profile) ----
// Открывает поиск писем, заходит в первое, ищет ссылку по confirmLinkRegex.
Func<string> getConfirmLinkGmailWeb = () => {
    try {
        instance.ActiveTab.Navigate(gmailSearchUrl, "");
        instance.ActiveTab.WaitDownloading();
        System.Threading.Thread.Sleep(3000);   // Gmail дорисовывает список асинхронно

        // Если Gmail попросил войти — значит сессии в профиле нет.
        string url = instance.ActiveTab.URL.ToLower();
        if (url.Contains("signin") || url.Contains("servicelogin")) {
            project.SendWarningToLog("Gmail не авторизован в профиле — подготовьте prepare_google_profile.cs", true);
            return null;
        }

        var mail = instance.ActiveTab.FindElementByXPath(xpGmailFirstMail, 0);
        if (mail.IsVoid) return null;           // писем ещё нет
        mail.Click();
        instance.ActiveTab.WaitDownloading();
        System.Threading.Thread.Sleep(2000);

        var link = System.Text.RegularExpressions.Regex.Match(
            instance.ActiveTab.DocumentText, confirmLinkRegex);
        return link.Success ? link.Value : null;
    } catch (Exception ex) {
        project.SendWarningToLog("Gmail-web ошибка: " + ex.Message, true);
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

// Проверка живости ТЕКУЩЕЙ прокси инстанса (ZP 7.7.2): навигация на ip-эхо.
// Не зависит от перегрузок HttpGet — используем уже настроенный прокси браузера.
Func<bool> checkProxyAlive = () => {
    try {
        instance.ActiveTab.Navigate("https://api.ipify.org/", "");
        instance.ActiveTab.WaitDownloading();
        var body = instance.ActiveTab.FindElementByTagName("body", 0);
        string ip = body.IsVoid ? "" : body.GetAttribute("innertext").Trim();
        bool ok = System.Text.RegularExpressions.Regex.IsMatch(ip, @"^\d{1,3}(\.\d{1,3}){3}$");
        if (ok) project.SendInfoToLog("IP через прокси: " + ip, false);
        return ok;
    } catch (Exception ex) {
        project.SendWarningToLog("Прокси-чек упал: " + ex.Message, false);
        return false;
    }
};

// Round-robin из списка (fallback, если аккаунту не назначена своя прокси).
// Ставит следующую живую прокси; возвращает применённую строку или null.
Func<string> applyNextProxy = () => {
    if (proxies.Count == 0) { instance.SetProxy(""); return proxyRequired ? null : ""; }
    for (int t = 0; t < Math.Min(proxyMaxTry, proxies.Count); t++) {
        string prx = proxies[proxyIdx % proxies.Count];
        proxyIdx++;
        instance.SetProxy(prx);
        if (checkProxyAlive()) { project.SendInfoToLog("Прокси OK: " + prx, false); return prx; }
        project.SendWarningToLog("Прокси не отвечает: " + prx, false);
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
    string email       = col(row, "email");
    string emailPass   = col(row, "email_password");         // пароль от почты
    string sitePassCsv = col(row, "password");               // отдельный пароль на checko (опц.)
    string appPw       = col(row, "email_app_password");     // пароль приложения для IMAP

    // Какой пароль подставить в форму регистрации checko:
    string pass;
    if (siteSameAsEmailPassword) {
        pass = !string.IsNullOrEmpty(emailPass) ? emailPass : sitePassCsv;
        if (string.IsNullOrEmpty(emailPass))
            project.SendWarningToLog("email_password пуст — использую password для " + email, true);
    } else {
        pass = sitePassCsv;
    }

    string profilePath = col(row, "profile");
    string accProxy    = col(row, "proxy");   // липкая прокси, назначенная аккаунту
    if (string.IsNullOrEmpty(appPw)) appPw = imapAppPasswordDef;

    project.SendInfoToLog("=== Регистрация: " + email + " ===", true);
    try
    {
        bool hasProfile = loadProfile && !string.IsNullOrEmpty(profilePath) && System.IO.File.Exists(profilePath);

        // --- Профиль / отпечаток / куки ---
        if (hasProfile) {
            // Сохранённая сессия (Gmail/Google): грузим как есть, отпечаток и куки НЕ трогаем.
            instance.LoadProfileFromFile(profilePath);
            project.SendInfoToLog("Профиль загружен: " + profilePath, false);
        } else {
            if (!string.IsNullOrEmpty(profilePath) && emailReadMode == "gmail_web") {
                // для gmail_web профиль обязателен
                fail++; report.AppendLine(email + ";SKIP;нет профиля " + profilePath);
                project.SendWarningToLog("Нет профиля для gmail_web: " + profilePath +
                    " — подготовьте prepare_google_profile.cs", true);
                continue;
            }
            if (regenProfilePerAccount) instance.GenerateNewProfile();  // свежий UA + отпечаток
            instance.ClearCookie();                                     // чистое состояние между аккаунтами
        }
        try { project.SendInfoToLog("UA: " + instance.Profile.UserAgent, false); } catch {}

        // --- Прокси: липкая на аккаунт (колонка proxy), иначе round-robin из списка ---
        string usedProxy;
        if (!string.IsNullOrEmpty(accProxy)) {
            instance.SetProxy(accProxy);
            usedProxy = checkProxyAlive() ? accProxy : null;   // назначенную НЕ подменяем на чужой IP
            if (usedProxy == null && proxyRequired) {
                fail++; report.AppendLine(email + ";SKIP;назначенная прокси недоступна: " + accProxy);
                project.SendWarningToLog("Пропуск " + email + ": прокси " + accProxy + " недоступна", true);
                continue;
            }
        } else {
            usedProxy = applyNextProxy();
            if (usedProxy == null && proxyRequired) {
                fail++; report.AppendLine(email + ";SKIP;нет живой прокси");
                project.SendWarningToLog("Пропуск " + email + ": нет живой прокси", true);
                continue;
            }
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
            link = (emailReadMode == "gmail_web")
                 ? getConfirmLinkGmailWeb()
                 : getConfirmLink(email, appPw, mailFromFilter);
            if (link != null) break;
            project.SendInfoToLog("Жду письмо (" + emailReadMode + ")... " + waited + "s", false);
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
