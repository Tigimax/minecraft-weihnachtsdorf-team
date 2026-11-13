void async function() {
    console.log("[Auth/info] Authentication integration loaded!");

    let authData = { loggedIn: false };
    try {
        const res = await fetch("/addons/integration/whoami");
        if (res.ok) {
            const data = await res.json();
            if (data.username && data.uuid) {
                authData = { loggedIn: true, uuid: data.uuid, username: data.username };
            }
        }
    } catch (e) {
        console.warn("[Auth/warn] Failed to fetch auth status", e);
    }

    window.bluemapAuth = authData;

    if (authData.loggedIn) {
        const userBadge = document.createElement("div");
        userBadge.id = "bluemap-user-badge";
        userBadge.style.display = "flex";
        userBadge.style.flexDirection = "row";
        userBadge.style.alignItems = "center";
        userBadge.style.padding = ".4em .8em";
        userBadge.style.fontFamily = "'minecraft', sans-serif";
        userBadge.style.fontSize = "15px";
        userBadge.style.color = "#fff";

        const headImg = document.createElement("img");
        const mapId = window.bluemap?.mapViewer?.data?.map?.id || "overworld";
        headImg.src = "/addons/integration/playerhead/" + encodeURIComponent(mapId);
        headImg.style.width = "24px";
        headImg.style.height = "24px";
        headImg.style.marginRight = "8px";
        headImg.style.borderRadius = "3px";
        headImg.alt = authData.username || "";

        const nameSpan = document.createElement("span");
        nameSpan.textContent = authData.username || "";

        userBadge.appendChild(headImg);
        userBadge.appendChild(nameSpan);

        const checkSideMenu = setInterval(() => {
            const buttonList = document.querySelector(".side-menu .content")?.children?.item(0);
            if (buttonList && !document.querySelector("#bluemap-user-badge")) {
                buttonList.insertBefore(userBadge, buttonList.firstChild);
                clearInterval(checkSideMenu);
            }
        }, 100);
        setTimeout(() => clearInterval(checkSideMenu), 10000);
    }
}();
