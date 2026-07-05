// ============================================================================
//  Кубик «Свой код C#»  —  04_save_profile
//  Сохраняет текущий профиль инстанса (fingerprint + cookies) в файл,
//  чтобы в следующий раз загрузить готовый профиль вместо генерации нового.
//  Файл: data/profiles/<uid>.zpprofile
//
//  Загрузить обратно: штатный кубик «Профиль → Загрузить из файла» с путём
//  {-Project.Directory-}\data\profiles\{-Variable.profileFile-}
// ============================================================================

string dir = System.IO.Path.Combine(project.Directory, "data", "profiles");
if (!System.IO.Directory.Exists(dir))
    System.IO.Directory.CreateDirectory(dir);

// Уникальное имя без Random/DateTime.Now зависимостей потока — на основе GUID.
string uid = Guid.NewGuid().ToString("N").Substring(0, 12);
string file = System.IO.Path.Combine(dir, uid + ".zpprofile");

// Сохранение профиля инстанса (fingerprint, cookies, WebGL/Canvas-настройки).
// ⚠ Набор/порядок аргументов SaveProfileToFile отличается между сборками ZP.
//   Если компилятор ругается — используйте штатный кубик «Профиль → Сохранить
//   в файл» (он всегда соответствует вашей версии), а этот код удалите.
instance.SaveProfileToFile(
    file,
    /* cookie      */ true,
    /* localStorage*/ true,
    /* flash/plugin*/ true,
    /* fingerprint */ true,
    /* proxy       */ false,   // прокси не пишем в профиль — он берётся из файла
    /* password    */ true,
    /* indexedDb   */ true);

project.Variables["profileFile"].Value = uid + ".zpprofile";
project.SendInfoToLog("Профиль сохранён: " + file, false);

return file;
