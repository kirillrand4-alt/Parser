// ============================================================================
//  Кубик «Свой код C#»  —  01_proxy_from_file
//  Берёт ПЕРВУЮ рабочую строку из data/proxies.txt, ГАРАНТИРОВАННО УДАЛЯЕТ её
//  из файла (атомарная запись + блокировка между потоками) и раскладывает
//  прокси по переменным: proxy, proxyScheme, proxyIp, proxyPort,
//  proxyLogin, proxyPassword.
//
//  В лог печатается «было N строк -> стало M» — так видно, что удаление прошло.
// ============================================================================

string path = System.IO.Path.Combine(project.Directory, "data", "proxies.txt");

string mutexName = "ZP_PROXY_" + path.Replace("\\", "_").Replace("/", "_").Replace(":", "_");

string rawLine = null;

using (var mtx = new System.Threading.Mutex(false, mutexName))
{
    bool got = false;
    try { got = mtx.WaitOne(TimeSpan.FromSeconds(30)); }
    catch (System.Threading.AbandonedMutexException) { got = true; }

    try
    {
        if (!System.IO.File.Exists(path))
            throw new Exception("Файл с прокси не найден: " + path);

        var lines = new System.Collections.Generic.List<string>(
            System.IO.File.ReadAllLines(path, System.Text.Encoding.UTF8));
        int before = lines.Count;

        // Первая непустая строка (комментарии '#' пропускаем).
        int idx = -1;
        for (int i = 0; i < lines.Count; i++)
        {
            string t = lines[i].Trim();
            if (t.Length == 0 || t.StartsWith("#")) continue;
            idx = i; break;
        }

        if (idx < 0)
            throw new Exception("Прокси закончились (файл пуст): " + path);

        rawLine = lines[idx].Trim();
        lines.RemoveAt(idx);                                   // удаляем взятую строку

        // Атомарная запись: сначала во временный файл, затем замена основного.
        string tmp = path + ".tmp";
        System.IO.File.WriteAllLines(tmp, lines, new System.Text.UTF8Encoding(false));
        if (System.IO.File.Exists(path)) System.IO.File.Delete(path);
        System.IO.File.Move(tmp, path);

        project.SendInfoToLog("proxies.txt: было " + before + " -> стало " + lines.Count +
                              " (удалена строка: " + rawLine + ")", false);
    }
    finally
    {
        if (got) mtx.ReleaseMutex();
    }
}

// ---------------------------------------------------------------------------
//  Разбор прокси: ip:port | ip:port:login:password | login:password@ip:port
//  с опциональной схемой http:// | https:// | socks4:// | socks5://
// ---------------------------------------------------------------------------
string scheme = "http";
string body = rawLine;

int schemeSep = body.IndexOf("://");
if (schemeSep >= 0)
{
    scheme = body.Substring(0, schemeSep).ToLowerInvariant();
    body = body.Substring(schemeSep + 3);
}

string login = "", password = "", ip = "", port = "";

if (body.Contains("@"))
{
    var parts = body.Split('@');
    var cred = parts[0].Split(':');
    var host = parts[1].Split(':');
    login = cred.Length > 0 ? cred[0] : "";
    password = cred.Length > 1 ? cred[1] : "";
    ip = host.Length > 0 ? host[0] : "";
    port = host.Length > 1 ? host[1] : "";
}
else
{
    var p = body.Split(':');
    ip   = p.Length > 0 ? p[0] : "";
    port = p.Length > 1 ? p[1] : "";
    if (p.Length >= 4) { login = p[2]; password = p[3]; }
}

if (string.IsNullOrEmpty(ip) || string.IsNullOrEmpty(port))
    throw new Exception("Не удалось разобрать прокси: '" + rawLine + "'");

string normalized = scheme + "://";
if (!string.IsNullOrEmpty(login))
    normalized += login + ":" + password + "@";
normalized += ip + ":" + port;

project.Variables["proxy"].Value        = normalized;
project.Variables["proxyScheme"].Value  = scheme;
project.Variables["proxyIp"].Value      = ip;
project.Variables["proxyPort"].Value    = port;
project.Variables["proxyLogin"].Value   = login;
project.Variables["proxyPassword"].Value= password;

return normalized;
