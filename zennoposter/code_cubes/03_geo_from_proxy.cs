// ============================================================================
//  Кубик «Свой код C#»  —  03_geo_from_proxy
//  Определяет страну / часовой пояс / язык по IP прокси (запрос идёт ЧЕРЕЗ прокси)
//  и кладёт их в переменные: geoCountry, geoTimezone, geoLang, geoIp.
//  Эти значения дальше применяются к профилю (таймзона + Accept-Language + WebRTC),
//  чтобы отпечаток был согласован с гео IP — главный признак «живого» юзера.
//
//  ⚠ WebProxy в .NET умеет только HTTP/HTTPS-прокси. Для SOCKS используйте
//    штатную возможность профиля «Определить гео по прокси» (она поддерживает
//    socks) — этот кубик тогда просто пропустит определение и оставит дефолт.
// ============================================================================

string ip       = project.Variables["proxyIp"].Value;
string port     = project.Variables["proxyPort"].Value;
string login    = project.Variables["proxyLogin"].Value;
string password = project.Variables["proxyPassword"].Value;
string scheme   = project.Variables["proxyScheme"].Value;

// Значения по умолчанию (если определить не удалось).
string geoCountry  = "US";
string geoTimezone = "America/New_York";
string geoLang     = "en-US,en;q=0.9";
string geoIp       = ip;

bool isHttpProxy = scheme == "http" || scheme == "https" || string.IsNullOrEmpty(scheme);

if (isHttpProxy && !string.IsNullOrEmpty(ip))
{
    try
    {
        var proxy = new System.Net.WebProxy(ip + ":" + port, true);
        if (!string.IsNullOrEmpty(login))
            proxy.Credentials = new System.Net.NetworkCredential(login, password);

        var req = (System.Net.HttpWebRequest)System.Net.WebRequest.Create(
            "http://ip-api.com/json/?fields=status,countryCode,timezone,query");
        req.Proxy = proxy;
        req.Timeout = 20000;
        req.UserAgent = "Mozilla/5.0";

        string json;
        using (var resp = req.GetResponse())
        using (var sr = new System.IO.StreamReader(resp.GetResponseStream()))
            json = sr.ReadToEnd();

        // Лёгкий разбор без внешних библиотек.
        Func<string, string> field = (name) =>
        {
            var m = System.Text.RegularExpressions.Regex.Match(
                json, "\"" + name + "\"\\s*:\\s*\"([^\"]*)\"");
            return m.Success ? m.Groups[1].Value : "";
        };

        string status = field("status");
        if (status == "success")
        {
            string cc = field("countryCode");
            string tz = field("timezone");
            string q  = field("query");
            if (!string.IsNullOrEmpty(cc)) geoCountry  = cc;
            if (!string.IsNullOrEmpty(tz)) geoTimezone = tz;
            if (!string.IsNullOrEmpty(q))  geoIp       = q;

            geoLang = LangByCountry(geoCountry);
        }
    }
    catch (Exception ex)
    {
        project.SendWarningToLog("Гео по прокси не определилось, беру дефолт. " + ex.Message, false);
    }
}
else
{
    project.SendInfoToLog("SOCKS-прокси: определите гео штатным кубиком профиля «Гео по прокси».", false);
}

project.Variables["geoCountry"].Value  = geoCountry;
project.Variables["geoTimezone"].Value = geoTimezone;
project.Variables["geoLang"].Value     = geoLang;
project.Variables["geoIp"].Value       = geoIp;

project.SendInfoToLog(
    "Гео: " + geoCountry + " | TZ=" + geoTimezone + " | lang=" + geoLang, false);

return geoCountry;

// ---------------------------------------------------------------------------
//  Страна → строка Accept-Language (навигаторный язык под гео).
// ---------------------------------------------------------------------------
string LangByCountry(string cc)
{
    switch (cc.ToUpperInvariant())
    {
        case "RU": return "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7";
        case "UA": return "uk-UA,uk;q=0.9,ru;q=0.8,en;q=0.7";
        case "US": return "en-US,en;q=0.9";
        case "GB": return "en-GB,en;q=0.9";
        case "DE": return "de-DE,de;q=0.9,en;q=0.8";
        case "FR": return "fr-FR,fr;q=0.9,en;q=0.8";
        case "ES": return "es-ES,es;q=0.9,en;q=0.8";
        case "IT": return "it-IT,it;q=0.9,en;q=0.8";
        case "PL": return "pl-PL,pl;q=0.9,en;q=0.8";
        case "BR": return "pt-BR,pt;q=0.9,en;q=0.8";
        case "TR": return "tr-TR,tr;q=0.9,en;q=0.8";
        case "NL": return "nl-NL,nl;q=0.9,en;q=0.8";
        case "IN": return "en-IN,en;q=0.9,hi;q=0.8";
        case "CN": return "zh-CN,zh;q=0.9,en;q=0.8";
        case "JP": return "ja-JP,ja;q=0.9,en;q=0.8";
        case "KR": return "ko-KR,ko;q=0.9,en;q=0.8";
        case "CA": return "en-CA,en;q=0.9,fr-CA;q=0.8";
        default:   return "en-US,en;q=0.9";
    }
}
