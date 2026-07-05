// ============================================================================
//  Кубик «Свой код C#»  —  05_save_api_key
//  Вытаскивает данные из блока «Ваш API-ключ» на странице
//  https://checko.ru/user/account/api и сохраняет их в файл (потокобезопасно).
//
//  Со страницы берём:
//    • API-ключ           — <div class="code">jBGhUK88hv5PS6gK</div>
//    • Тарифный план       — <div class="h2 mt-0">«Лайт»</div>
//    • E-mail аккаунта     — блок .user-email / .text-muted
//
//  Результат дописывается строкой в data/api_keys.txt в формате:
//    email | apiKey | tariff | proxyIp:port | yyyy... (дата ставится в ZP-переменной)
//
//  Перед этим кубиком должен идти переход на страницу:
//    кубик «Перейти по URL» → https://checko.ru/user/account/api
//  (аккаунт уже залогинен в текущем профиле).
// ============================================================================

// 1) HTML активной вкладки.
string html = instance.ActiveTab.DocumentText;
if (string.IsNullOrEmpty(html))
    throw new Exception("Пустой DocumentText — страница /user/account/api не загрузилась");

Func<string, string> match = (pattern) =>
{
    var m = System.Text.RegularExpressions.Regex.Match(
        html, pattern,
        System.Text.RegularExpressions.RegexOptions.Singleline |
        System.Text.RegularExpressions.RegexOptions.IgnoreCase);
    return m.Success ? m.Groups[1].Value.Trim() : "";
};

// 2) API-ключ: первый <div class="code">...</div>.
string apiKey = match("<div\\s+class=\"code\"[^>]*>\\s*([^<]+?)\\s*</div>");
if (string.IsNullOrEmpty(apiKey))
    throw new Exception("API-ключ не найден на странице (изменилась вёрстка?)");

// 3) Тарифный план: <div class="h2 mt-0">«Лайт»</div> — чистим кавычки-ёлочки.
string tariff = match("<div\\s+class=\"h2[^\"]*\"[^>]*>\\s*([^<]+?)\\s*</div>")
                    .Replace("«", "").Replace("»", "").Trim();

// 4) E-mail: сначала из .user-email, затем из .text-muted.
string email = match("class=\"user-email\">.*?<div>\\s*([^<]+?)\\s*</div>");
if (string.IsNullOrEmpty(email))
    email = match("<div\\s+class=\"text-muted\">\\s*([^<@\\s]+@[^<\\s]+?)\\s*</div>");

// 5) Прокси (для привязки ключа к IP) — из переменных кубика 01.
string proxyIp = "";
try { proxyIp = project.Variables["proxyIp"].Value + ":" + project.Variables["proxyPort"].Value; }
catch { }

// 6) Кладём в переменные проекта (пригодятся дальше по шаблону).
project.Variables["apiKey"].Value  = apiKey;
project.Variables["tariff"].Value  = tariff;
project.Variables["apiEmail"].Value= email;

// 7) Потокобезопасная дозапись в общий файл.
string dir  = System.IO.Path.Combine(project.Directory, "data");
if (!System.IO.Directory.Exists(dir)) System.IO.Directory.CreateDirectory(dir);
string path = System.IO.Path.Combine(dir, "api_keys.txt");

string line = string.Join(" | ", new string[] { email, apiKey, tariff, proxyIp }) + Environment.NewLine;

string mutexName = "ZP_APIKEYS_" + path.Replace("\\", "_").Replace("/", "_").Replace(":", "_");
using (var mtx = new System.Threading.Mutex(false, mutexName))
{
    bool got = false;
    try { got = mtx.WaitOne(TimeSpan.FromSeconds(30)); }
    catch (System.Threading.AbandonedMutexException) { got = true; }
    try
    {
        System.IO.File.AppendAllText(path, line, new System.Text.UTF8Encoding(false));
    }
    finally { if (got) mtx.ReleaseMutex(); }
}

project.SendInfoToLog("API-ключ сохранён: " + apiKey + " (" + tariff + ", " + email + ")", false);
return apiKey;
