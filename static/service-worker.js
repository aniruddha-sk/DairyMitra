/* =====================================
   DairyMitr Service Worker
   Production Ready v23
===================================== */

const CACHE_NAME = "dairy-mitr-cache-v23";


/* =====================================
   APP SHELL
===================================== */

const APP_SHELL = [

    "/static/css/style.css",
    "/static/css/receipt.css",
    "/static/css/about.css",
    "/static/manifest.json",

    "/static/images/logo.png"

];


/* =====================================
   INSTALL
===================================== */

self.addEventListener("install", (event) => {

    event.waitUntil(

        caches.open(CACHE_NAME)

            .then(async (cache) => {

                for (const file of APP_SHELL) {

                    try {

                        await cache.add(file);

                        console.log(
                            "Cached:",
                            file
                        );

                    } catch (err) {

                        console.log(
                            "Skipped:",
                            file
                        );

                    }

                }

            })

    );

    /*
     * Activate new SW immediately
     */
    self.skipWaiting();

});


/* =====================================
   ACTIVATE
===================================== */

self.addEventListener("activate", (event) => {

    event.waitUntil(

        caches.keys()

            .then((keys) => {

                return Promise.all(

                    keys.map((key) => {

                        /*
                         * Delete all old DairyMitr caches
                         */
                        if (
                            key.startsWith("dairy-mitr-cache-") &&
                            key !== CACHE_NAME
                        ) {

                            console.log(
                                "Deleting old cache:",
                                key
                            );

                            return caches.delete(key);

                        }

                    })

                );

            })

            .then(() => {

                /*
                 * Take control of all open tabs
                 */
                return self.clients.claim();

            })

    );

});


/* =====================================
   FETCH
===================================== */

self.addEventListener("fetch", (event) => {

    const request = event.request;


    /*
     * Only GET requests
     */
    if (request.method !== "GET") {
        return;
    }


    const url = new URL(request.url);


    /* =================================
       HTML / PAGE REQUESTS
    ================================= */

    /*
     * NEVER cache HTML pages.
     *
     * Always get latest version
     * from server.
     */
    if (request.mode === "navigate") {

        event.respondWith(

            fetch(request)

                .catch(() => {

                    return caches.match(
                        "/"
                    );

                })

        );

        return;

    }


    /* =================================
       LOGIN / API / DYNAMIC ROUTES
    ================================= */

    if (

        url.pathname.startsWith("/login") ||
        url.pathname.startsWith("/logout") ||
        url.pathname.startsWith("/customer") ||
        url.pathname.startsWith("/staff") ||
        url.pathname.startsWith("/subscribe") ||
        url.pathname.startsWith("/advance") ||
        url.pathname.startsWith("/milk") ||
        url.pathname.startsWith("/payment")

    ) {

        event.respondWith(
            fetch(request)
        );

        return;

    }


    /* =================================
       STATIC FILES
    ================================= */

    if (

        request.destination === "style" ||
        request.destination === "script" ||
        request.destination === "image" ||
        request.destination === "font"

    ) {

        event.respondWith(

            caches.match(request)

                .then((cached) => {

                    /*
                     * If cached version exists,
                     * return it immediately.
                     */
                    if (cached) {

                        /*
                         * Background update
                         *
                         * Fetch latest file in background.
                         */
                        fetch(request)

                            .then((response) => {

                                if (
                                    response &&
                                    response.status === 200
                                ) {

                                    caches.open(
                                        CACHE_NAME
                                    ).then((cache) => {

                                        cache.put(
                                            request,
                                            response.clone()
                                        );

                                    });

                                }

                            })
                            .catch(() => {});

                        return cached;

                    }


                    /*
                     * Not cached:
                     * fetch from server.
                     */
                    return fetch(request)

                        .then((response) => {

                            if (
                                response &&
                                response.status === 200
                            ) {

                                const copy =
                                    response.clone();

                                caches.open(
                                    CACHE_NAME
                                ).then((cache) => {

                                    cache.put(
                                        request,
                                        copy
                                    );

                                });

                            }

                            return response;

                        });

                })

        );

    }

});


/* =====================================
   PUSH NOTIFICATION
===================================== */

self.addEventListener("push", (event) => {

    if (!event.data) {
        return;
    }


    let data;


    try {

        data = event.data.json();

    } catch {

        data = {

            title: "DairyMitr",

            body: event.data.text()

        };

    }


    event.waitUntil(

        self.registration.showNotification(

            data.title || "DairyMitr",

            {

                body:
                    data.body || "",

                icon:
                    "/static/images/logo.png",

                badge:
                    "/static/images/logo.png",

                vibrate:
                    [200, 100, 200],

                data: {

                    url:
                        data.url ||
                        "/customer/dashboard"

                }

            }

        )

    );

});


/* =====================================
   NOTIFICATION CLICK
===================================== */

self.addEventListener(
    "notificationclick",
    (event) => {

        event.notification.close();


        const url =
            event.notification.data.url ||
            "/customer/dashboard";


        event.waitUntil(

            clients.matchAll({

                type: "window",

                includeUncontrolled: true

            })

            .then((windowClients) => {

                for (
                    const client
                    of windowClients
                ) {

                    if (
                        client.url.includes(url)
                    ) {

                        return client.focus();

                    }

                }


                return clients.openWindow(
                    url
                );

            })

        );

    }
);