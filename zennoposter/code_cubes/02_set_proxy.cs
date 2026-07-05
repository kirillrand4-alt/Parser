// ============================================================================
//  Кубик «Свой код C#»  —  02_set_proxy
//  Применяет прокси к текущему инстансу браузера.
//  Берёт нормализованную строку из переменной proxy (её готовит кубик 01).
//
//  Формат, который понимает instance.SetProxy:
//     scheme://login:password@ip:port   (scheme = http/https/socks4/socks5)
//  или ip:port  (без авторизации)
// ============================================================================

string proxy = project.Variables["proxy"].Value;

if (string.IsNullOrWhiteSpace(proxy))
    throw new Exception("Переменная proxy пуста — сначала выполните кубик 01_proxy_from_file");

// SetProxy принимает строку целиком, включая схему и авторизацию.
// Второй параметр — не сбрасывать прокси при рестарте инстанса (true = держать).
instance.SetProxy(proxy, true);

project.SendInfoToLog("Прокси применён к инстансу: " + proxy, false);

// (Опционально) проверка внешнего IP после установки прокси — раскомментируйте,
// если хотите убедиться, что трафик реально идёт через прокси:
//
// instance.ActiveTab.Navigate("https://api.ipify.org", "");
// string ext = instance.ActiveTab.DocumentText;
// project.SendInfoToLog("Внешний IP через прокси: " + ext, false);
// project.Variables["externalIp"].Value = ext;

return proxy;
