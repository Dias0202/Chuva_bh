"""Entrada do pacote no AgentCore: roda da raiz para que `agent` seja importável."""

from agent.app import app

if __name__ == "__main__":
    app.run()
