# Lab: Secrets, SecretProviderClass, Service Accounts and Azure Key Vault on AKS

**Estimated time:** 60-90 min
**Prerequisites:** `az` CLI logged in (`az login`), an AKS cluster, `kubectl` pointing at it, the `ecommerce-dev` namespace from your Helm charts, and permission to create Key Vaults / identities / role assignments in the resource group.

This lab uses your ecommerce app throughout. The goal: stop storing `SESSION_SECRET` in git/Helm values and instead pull it from Azure Key Vault at runtime, first via the Secrets Store CSI driver (SecretProviderClass), then via Workload Identity.

```bash
# Set once per session
export RG=<resource-group>
export CLUSTER=<aks-cluster-name>
export LOCATION=<eastus>
export KV=<unique-keyvault-name>
export NS=ecommerce-dev

az aks get-credentials -g $RG -n $CLUSTER
```

---

## Exercise 1 - Plain Kubernetes Secrets (the baseline)

Goal: understand what a Secret is and what it is *not*.

1. Create one from a literal and decode it back:

   ```bash
   kubectl create secret generic frontend-secret \
     --from-literal=SESSION_SECRET=$(openssl rand -hex 32) -n $NS

   kubectl get secret frontend-secret -n $NS -o jsonpath='{.data.SESSION_SECRET}' | base64 -d
   ```

2. Look at how your Helm chart injects it: `argocd/helm/frontend/templates/secret.yaml` + the `secretKeyRef` in `templates/deployment.yaml`. Note that `stringData` is base64-encoded at rest in etcd only - anyone with `get secrets` RBAC can read it.

3. **Your task:** delete the secret, recreate it with a different value, and confirm the running frontend pod does *not* see the new value until you `helm upgrade`/restart it.

   ```bash
   kubectl delete secret frontend-secret -n $NS
   kubectl create secret generic frontend-secret \
     --from-literal=SESSION_SECRET=$(openssl rand -hex 32) -n $NS
   kubectl rollout restart deploy frontend-deploy -n $NS
   ```

**Takeaway:** k8s Secrets are base64, not encryption, and they live in git/etcd. That is why production designs move them to Key Vault.

---

## Exercise 2 - ServiceAccount + RBAC

Goal: a pod that is allowed to read secrets, and one that is not.

1. Create a dedicated SA and a role that can only list/get secrets:

   ```bash
   kubectl create serviceaccount reader -n $NS

   kubectl create role secret-reader \
     --verb=get,list --resource=secrets -n $NS

   kubectl create rolebinding reader-binding \
     --role=secret-reader \
     --serviceaccount=$NS:reader -n $NS
   ```

2. Check permissions as both identities:

   ```bash
   kubectl auth can-i list secrets \
     --as=system:serviceaccount:$NS:reader -n $NS        # yes

   kubectl auth can-i list secrets \
     --as=system:serviceaccount:$NS:default -n $NS       # no
   ```

3. **Your task:** run a shell as the `reader` SA and list secrets, then repeat with `default`:

   ```bash
   kubectl run can-i -n $NS --rm -it --image=bitnami/kubectl:latest \
     --serviceaccount=reader -- get secrets
   ```

**Takeaway:** the ServiceAccount is the pod's identity *inside* the cluster; RBAC decides what that identity may do.

---

## Exercise 3 - Azure Key Vault + Secrets Store CSI driver (SecretProviderClass)

Goal: mount Key Vault secrets as files in the pod. This matches the `*_FILE` convention your app already implements (`App/frontend/app/main.py:12-22`: `SESSION_SECRET_FILE` and `DATABASE_URL_FILE` beat plain env vars).

1. Create the vault and put two secrets in it:

   ```bash
   az keyvault create -g $RG -n $KV -l $LOCATION \
     --enable-rbac-authorization true

   az keyvault secret set --vault-name $KV -n session-secret -value $(openssl rand -hex 32)
   az keyvault secret set --vault-name $KV -n database-url \
     -value "mssql+pyodbc://sa:Ecommerce-dev-0nly@mssql:1433/ecommerce?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes"
   ```

2. Enable the CSI add-on on the cluster and grant the kubelet identity read access:

   ```bash
   az aks enable-addons -g $RG -n $CLUSTER -a azure-keyvault-secrets-provider

   KUBELET_ID=$(az aks show -g $RG -n $CLUSTER \
     --query identityProfile.kubeletidentity.clientId -o tsv)

   az role assignment create --assignee $KUBELET_ID \
     --role "Key Vault Secrets User" \
     --scope $(az keyvault show -g $RG -n $KV --query id -o tsv)
   ```

3. Create the SecretProviderClass (note the class is namespaced):

   ```bash
   cat <<EOF | kubectl apply -f -
   apiVersion: secrets-store.csi.x-k8s.io/v1
   kind: SecretProviderClass
   metadata:
     name: azure-kv-class
     namespace: $NS
   spec:
     provider: azure
     parameters:
       usePodIdentity: "false"
       keyvaultName: "$KV"
       objects: |
         array:
           - |
             objectName: session-secret
             objectType: secret
             objectVersion: ""
           - |
             objectName: database-url
             objectType: secret
             objectVersion: ""
       tenantId: "$(az account show --query tenantId -o tsv)"
   EOF
   ```

4. **Your task:** patch a test pod to mount the class and read the files:

   ```bash
   cat <<EOF | kubectl apply -f -
   apiVersion: v1
   kind: Pod
   metadata:
     name: kv-test
     namespace: $NS
   spec:
     containers:
       - name: demo
         image: alpine:latest
         command: ["sleep", "infinity"]
         volumeMounts:
           - name: kv
             mountPath: /mnt/kv
             readOnly: true
     volumes:
       - name: kv
         csi:
           driver: secrets-store.csi.k8s.io
           readOnly: true
           volumeAttributes:
             secretProviderClass: azure-kv-class
   EOF

   kubectl wait --for=condition=ready pod/kv-test -n $NS --timeout=120s
   kubectl exec -n $NS kv-test -- ls /mnt/kv
   kubectl exec -n $NS kv-test -- cat /mnt/kv/session-secret
   ```

**Takeaway:** the secret never enters etcd, git, or your Helm values - the CSI driver fetches it over managed identity and mounts it as a file.

---

## Exercise 4 - Workload Identity (pod = Azure AD identity)

Goal: a pod that calls Azure APIs itself (here: `az keyvault secret show`) using a federated ServiceAccount, no stored credentials.

1. Enable the AKS features and create a user-assigned identity:

   ```bash
   az aks update -g $RG -n $CLUSTER --enable-oidc-issuer --enable-workload-identity

   ISSUER=$(az aks show -g $RG -n $CLUSTER --query oidcIssuerProfile.issuerUrl -o tsv)

   az identity create -g $RG -n kv-reader
   UAI_ID=$(az identity show -g $RG -n kv-reader --query clientId -o tsv)

   az identity federated-credential create -g $RG -n kv-reader \
     --name frontend-wi \
     --issuer "$ISSUER" \
     --subject system:serviceaccount:$NS:frontend

   az role assignment create --assignee $UAI_ID \
     --role "Key Vault Secrets User" \
     --scope $(az keyvault show -g $RG -n $KV --query id -o tsv)
   ```

2. Annotate the ServiceAccount and run azure-cli as it:

   ```bash
   cat <<EOF | kubectl apply -f -
   apiVersion: v1
   kind: ServiceAccount
   metadata:
     name: frontend
     namespace: $NS
     annotations:
       azure.workload.identity/client-id: "$UAI_ID"
     labels:
       azure.workload.identity/use: "true"
   EOF

   kubectl run kv-cli -n $NS --rm -it \
     --image=mcr.microsoft.com/azure-cli \
     --overrides='{"spec":{"serviceAccountName":"frontend"}}' \
     -- az keyvault secret show --name session-secret --vault-name $KV --query value -o tsv
   ```

**Takeaway:** the ServiceAccount token is exchanged for an Azure token via OIDC federation - no client secret anywhere.

---

## Capstone - wire the frontend to Key Vault

Do this in `argocd/helm/frontend/`, then let ArgoCD sync it:

1. Remove `templates/secret.yaml` and the `sessionSecret` value + `secretKeyRef` env block from the chart.
2. Add to `templates/deployment.yaml`:

   - volume `kv` (CSI, `secretProviderClass: azure-kv-class`) mounted at `/mnt/kv`
   - env `SESSION_SECRET_FILE: /mnt/kv/session-secret`
   - optionally `DATABASE_URL_FILE: /mnt/kv/database-url` on catalog/customer/order instead of `db-secret`
3. Commit, push, watch ArgoCD self-heal the drift.
4. Verify: `kubectl exec deploy/frontend-deploy -n $NS -- ls /mnt/kv` and that login still sets a session cookie (`/login`, then `/account/orders`).

**Discussion questions:**

1. The CSI driver updates the mounted file when the KV secret changes - does the running frontend pick it up? Why? (Hint: `SESSION_SECRET` is read once at import time, `App/frontend/app/main.py:28`.)
2. What breaks if you delete the `reader` ServiceAccount in Exercise 2 mid-pod? What about deleting the annotated `frontend` SA in Exercise 4?
3. Why is `usePodIdentity: "false"` + kubelet identity different from giving every pod its own identity?
4. Where would ArgoCD fit if the SecretProviderClass itself must differ per environment (dev/qa/prod vaults)?

---

## Cleanup

```bash
kubectl delete pod kv-test -n $NS
kubectl delete secret frontend-secret -n $NS
kubectl delete sa reader -n $NS
kubectl delete role secret-reader -n $NS
kubectl delete rolebinding reader-binding -n $NS
az identity delete -g $RG -n kv-reader -y
az keyvault delete -g $RG -n $KV
az role assignment delete --assignee $KUBELET_ID \
  --scope $(az keyvault show -g $RG -n $KV --query id -o tsv 2>/dev/null) # if vault still exists
```

If you used a scratch resource group, `az group delete -g $RG` removes everything.
