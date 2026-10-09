// ======================================
// DairyMitr Push Subscription (Debug Version)
// ======================================

function urlBase64ToUint8Array(base64String) {
    const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
    const base64 = (base64String + padding)
        .replace(/-/g, "+")
        .replace(/_/g, "/");

    const rawData = window.atob(base64);

    return Uint8Array.from([...rawData].map(c => c.charCodeAt(0)));
}

async function subscribeUser() {

    console.log("STEP 1 : subscribeUser() started");

    if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
        console.log("Push not supported");
        return;
    }

    try {

        const permission = await Notification.requestPermission();

        console.log("Permission =", permission);

        if (permission !== "granted") {
            return;
        }

        const registration = await navigator.serviceWorker.ready;

        console.log("STEP 2 : Service Worker Ready");

        let subscription =
            await registration.pushManager.getSubscription();

        if (!subscription) {

            console.log("Creating new subscription...");

            const keyRes = await fetch("/vapid_public_key");

            console.log("VAPID Status =", keyRes.status);

            const keyData = await keyRes.json();

            subscription =
                await registration.pushManager.subscribe({

                    userVisibleOnly: true,

                    applicationServerKey:
                        urlBase64ToUint8Array(keyData.publicKey)

                });

        } else {

            console.log("Existing subscription found");

        }

        console.log("Subscription =", subscription);

        const response = await fetch("/subscribe", {

            method: "POST",

            headers: {
                "Content-Type": "application/json"
            },

            body: JSON.stringify(subscription)

        });

        console.log("/subscribe Status =", response.status);

        console.log(await response.text());

    }
    catch (err) {

        console.error(err);

    }

}

navigator.serviceWorker
    .register("/service-worker.js")
    .then(() => {

        console.log("Service Worker Registered");

        subscribeUser();

    })
    .catch(console.error);