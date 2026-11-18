"""Gerador de tráfego que roda dentro do cluster (namespace loadtest).

Sem tráfego os SLIs ficam sem dados e os alertas nunca disparam
(aprendizado da primeira sessão de testes na AWS).
"""

import os
import random
import time
import urllib.request

TARGET = os.getenv("TARGET", "http://orders-api.orders")
RPS = float(os.getenv("RPS", "10"))

while True:
    try:
        if random.random() < 0.3:
            req = urllib.request.Request(
                f"{TARGET}/orders", data=b'{"item":"teclado","quantity":1}',
                headers={"content-type": "application/json"}, method="POST",
            )
        else:
            req = urllib.request.Request(f"{TARGET}/orders")
        urllib.request.urlopen(req, timeout=3).read()
    except Exception:
        pass  # erros são esperados durante os cenários
    time.sleep(1 / RPS)
