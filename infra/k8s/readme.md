# Kubernetes deployment

The `infra/k8s/deployment.yaml` file is simple and straight forward.
Before deploying it, you will need to configure the modem credentials in a k8s secret.

```shell
❯ cp modem_creds.ref modem_creds
❯ $EDITOR modem_creds
# Then create the secret
❯ k create secret generic sb-modem-creds --from-env-file=modem_creds --namespace=o11y
secret/sb-modem-creds created
```

Then deploy the exporter:

```shell
❯ k apply -f infra/k8s/deployment.yaml
deployment.apps/sb-exporter created
service/sb-exporter configured
scrapeconfig.monitoring.coreos.com/sb-exporter configured
```

The namespace, service name, deployment name ... etc are all representative of my environment and may need to be adjusted to suit yours.
