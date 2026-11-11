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
        const userTemplate = document.createElement("template");
        userTemplate.innerHTML = `
        <div id="bluemap-user-badge" style="display: flex; flex-direction: row; align-items: center; padding: .4em .8em; font-family: 'minecraft', sans-serif; font-size: 15px; color: #fff;">
            <img
                src="/addons/integration/playerhead/${window.bluemap?.mapViewer?.data?.map?.id || 'overworld'}"
                style="width: 24px; height: 24px; margin-right: 8px; border-radius: 3px;"
                alt="${authData.username}" />
            <span>${authData.username}</span>
        </div>
        `.trim();
        const userElement = userTemplate.content.firstChild;

        const checkSideMenu = setInterval(() => {
            const buttonList = document.querySelector(".side-menu .content")?.children?.item(0);
            if (buttonList && !document.querySelector("#bluemap-user-badge")) {
                buttonList.insertBefore(userElement, buttonList.firstChild);
                clearInterval(checkSideMenu);
            }
        }, 100);
        setTimeout(() => clearInterval(checkSideMenu), 10000);
    }
}();
