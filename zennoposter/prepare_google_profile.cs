// =============================================================================
//  ZennoPoster: подготовка профиля с авторизацией Google (РУЧНОЙ вход)
// =============================================================================
//  Запускается ОДИН раз на каждый Google-аккаунт.
//  Скрипт открывает страницу входа Google и ЖДЁТ, пока вы войдёте РУКАМИ
//  (логин, пароль, 2FA, капча — всё делаете вы). Затем сохраняет профиль
//  (куки-сессию) в файл. Основной регистратор потом просто загрузит профиль.
//
//  Почему так: автоматический ввод учётных данных в Google и обход его
//  проверок здесь НЕ делается (Google это блокирует и запрещает). Вход
//  выполняет человек; автоматика лишь переиспользует уже готовую сессию.
//
//  Куда вставлять: кубик "Свой код" (CodeCreator), запуск в ProjectMaker,
//                  чтобы видеть браузер и войти вручную.
// =============================================================================

// ------------------------------- SETTINGS ------------------------------------
string profilePath   = @"C:\Zenno\checko\profiles\account1.zpprofile"; // куда сохранить профиль
string googleLoginUrl= @"https://accounts.google.com/signin";
string loggedInUrl   = @"https://myaccount.google.com/";  // страница, доступная только после входа
int    waitLoginMin  = 10;    // сколько минут ждать ручной вход
int    pollSeconds   = 5;     // как часто проверять, вошли ли
// -----------------------------------------------------------------------------

// Создаём папку под профиль
try { System.IO.Directory.CreateDirectory(System.IO.Path.GetDirectoryName(profilePath)); } catch {}

instance.ActiveTab.Navigate(googleLoginUrl, "");
instance.ActiveTab.WaitDownloading();
project.SendInfoToLog("Войдите в Google ВРУЧНУЮ в открытом браузере. Жду до " + waitLoginMin + " мин...", true);

bool loggedIn = false;
int waited = 0, limit = waitLoginMin * 60;
while (waited < limit)
{
    System.Threading.Thread.Sleep(pollSeconds * 1000);
    waited += pollSeconds;

    // Проверяем сессию: открываем страницу личного кабинета.
    instance.ActiveTab.Navigate(loggedInUrl, "");
    instance.ActiveTab.WaitDownloading();
    string url = instance.ActiveTab.URL.ToLower();

    // Если НЕ перекинуло на страницу входа — значит уже авторизованы.
    if (!url.Contains("signin") && !url.Contains("servicelogin") && !url.Contains("accounts.google.com/v3/signin"))
    {
        loggedIn = true;
        break;
    }
    project.SendInfoToLog("Ещё не вошли... " + waited + "s", false);
}

if (!loggedIn)
    throw new Exception("Вход в Google не выполнен за отведённое время. Профиль не сохранён.");

instance.SaveProfileToFile(profilePath);
project.SendInfoToLog("Готово. Профиль с сессией Google сохранён: " + profilePath, true);
