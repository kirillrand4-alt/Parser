// =============================================================================
//  ZennoPoster: регистратор аккаунтов на B2B-портале поставщика
// =============================================================================
//  Назначение:  разовая регистрация нескольких СВОИХ аккаунтов на портале,
//               где у вас есть право заводить учётные записи.
//               Данные берутся из CSV-файла (свои реальные реквизиты).
//
//  Куда вставлять: кубик "Свой код" (CodeCreator) в проекте ZennoPoster.
//                  Тип действия — C# code. Требует ссылок:
//                  System, System.IO (добавляются в проекте по умолчанию).
//
//  Как настроить под конкретный портал:
//    1. Заполните константы в блоке SETTINGS ниже (URL и XPath полей).
//    2. Подготовьте CSV (см. accounts.example.csv) и укажите путь к нему.
//    3. XPath-селекторы подсмотрите в браузере ZennoPoster:
//       ПКМ по полю формы -> "Показать в HTML" -> скопировать XPath.
//
//  Логика: скрипт по очереди проходит по строкам CSV, для каждой открывает
//          форму регистрации, заполняет поля, отправляет и проверяет успех.
//          Результат по каждой строке дописывается в accounts_result.csv.
// =============================================================================

// ------------------------------- SETTINGS ------------------------------------
string csvPath        = @"C:\Zenno\b2b\accounts.csv";          // входной файл
string resultPath     = @"C:\Zenno\b2b\accounts_result.csv";   // отчёт
string regUrl         = @"https://postavshchik.ru/register";   // страница регистрации

// XPath полей формы — ПОДСТАВЬТЕ под ваш портал.
// Оставьте пустую строку "" для полей, которых на форме нет.
string xpCompany      = "//input[@name='company']";      // Название компании
string xpInn          = "//input[@name='inn']";          // ИНН
string xpContactName  = "//input[@name='fio']";          // Контактное лицо (ФИО)
string xpEmail        = "//input[@name='email']";        // Email
string xpPhone        = "//input[@name='phone']";        // Телефон
string xpPassword     = "//input[@name='password']";     // Пароль
string xpPassword2    = "//input[@name='password2']";    // Повтор пароля (если есть)
string xpAgreeCheckbox= "//input[@type='checkbox' and @name='agree']"; // Чекбокс согласия
string xpSubmit       = "//button[@type='submit']";      // Кнопка "Зарегистрироваться"

// Признак успешной регистрации: любой из этих XPath найден ИЛИ текст на странице.
string xpSuccessMark  = "//*[contains(@class,'reg-success')]"; // напр. блок "Спасибо"
string successText    = "спасибо за регистрацию";               // подстрока (в нижнем регистре)

bool typingEmulation  = true;   // true = посимвольный ввод (человечнее), false = вставка целиком
int  minDelayMs       = 800;    // пауза между действиями, мин
int  maxDelayMs       = 2200;   // пауза между действиями, макс
// -----------------------------------------------------------------------------

var rnd = new Random();
Action pause = () => System.Threading.Thread.Sleep(rnd.Next(minDelayMs, maxDelayMs));

// Заполнить одно поле по XPath (тихо пропускает, если xpath пустой или поле не найдено)
Action<string,string,string> fill = (xpath, value, label) => {
    if (string.IsNullOrEmpty(xpath) || value == null) return;
    var el = instance.ActiveTab.FindElementByXPath(xpath, 0);
    if (el.IsVoid) { project.SendWarningToLog("Поле не найдено: " + label + " (" + xpath + ")", true); return; }
    el.SetValue(value, typingEmulation ? "Full" : "None", typingEmulation);
    pause();
};

// --- Чтение CSV (первая строка — заголовок с именами колонок) ---
if (!System.IO.File.Exists(csvPath))
    throw new Exception("Не найден файл с данными: " + csvPath);

var lines = System.IO.File.ReadAllLines(csvPath, System.Text.Encoding.UTF8);
if (lines.Length < 2)
    throw new Exception("В CSV нет данных (нужен заголовок + минимум одна строка).");

var header = lines[0].Split(';');
Func<string[],string,string> col = (row, name) => {
    for (int i = 0; i < header.Length; i++)
        if (header[i].Trim().Equals(name, StringComparison.OrdinalIgnoreCase))
            return i < row.Length ? row[i].Trim() : "";
    return "";
};

var report = new System.Text.StringBuilder();
report.AppendLine("email;company;status;detail");
int ok = 0, fail = 0;

for (int r = 1; r < lines.Length; r++)
{
    if (string.IsNullOrWhiteSpace(lines[r])) continue;
    var row = lines[r].Split(';');

    string email = col(row, "email");
    string company = col(row, "company");
    project.SendInfoToLog("=== Регистрация: " + email + " (" + company + ") ===", true);

    try
    {
        instance.ActiveTab.Navigate(regUrl, "");
        instance.ActiveTab.WaitDownloading();
        pause();

        fill(xpCompany,     company,               "company");
        fill(xpInn,         col(row, "inn"),       "inn");
        fill(xpContactName, col(row, "contact"),   "contact");
        fill(xpEmail,       email,                 "email");
        fill(xpPhone,       col(row, "phone"),     "phone");
        fill(xpPassword,    col(row, "password"),  "password");
        fill(xpPassword2,   col(row, "password"),  "password2");

        // Чекбокс согласия
        if (!string.IsNullOrEmpty(xpAgreeCheckbox))
        {
            var chk = instance.ActiveTab.FindElementByXPath(xpAgreeCheckbox, 0);
            if (!chk.IsVoid && chk.GetAttribute("checked") != "true") { chk.Click(); pause(); }
        }

        // Отправка формы
        var btn = instance.ActiveTab.FindElementByXPath(xpSubmit, 0);
        if (btn.IsVoid) throw new Exception("Кнопка отправки не найдена: " + xpSubmit);
        btn.Click();
        instance.ActiveTab.WaitDownloading();
        pause();

        // Проверка успеха
        bool success = false;
        if (!string.IsNullOrEmpty(xpSuccessMark))
            success = !instance.ActiveTab.FindElementByXPath(xpSuccessMark, 0).IsVoid;
        if (!success && !string.IsNullOrEmpty(successText))
            success = instance.ActiveTab.DocumentText.ToLower().Contains(successText.ToLower());

        if (success)
        {
            ok++;
            report.AppendLine(email + ";" + company + ";OK;");
            project.SendInfoToLog("OK: " + email, true);
        }
        else
        {
            fail++;
            report.AppendLine(email + ";" + company + ";FAIL;не найден признак успеха");
            project.SendWarningToLog("Не подтверждена регистрация: " + email, true);
        }
    }
    catch (Exception ex)
    {
        fail++;
        report.AppendLine(email + ";" + company + ";ERROR;" + ex.Message.Replace(";", ","));
        project.SendErrorToLog("Ошибка на " + email + ": " + ex.Message, true);
    }

    pause();
}

System.IO.File.WriteAllText(resultPath, report.ToString(), System.Text.Encoding.UTF8);
project.SendInfoToLog("Готово. Успешно: " + ok + ", ошибок: " + fail + ". Отчёт: " + resultPath, true);
