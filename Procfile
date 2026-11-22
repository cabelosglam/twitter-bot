# IMPORTANTE: -w 1. Cada worker sobe o proprio scheduler; com mais de um
# worker o mesmo horario seria disparado varias vezes. As threads dao conta
# das requisicoes do painel.
web: gunicorn -w 1 --threads 4 --timeout 180 -b 0.0.0.0:$PORT app:app
