import { expect, test } from '@playwright/test';

test('mock finance stream, evidence, confirmation, and history survive refresh', async ({
  page,
}) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto('/');
  await expect(page.getByText('DEMO MODE', { exact: true })).toBeVisible();
  await expect(
    page.getByRole('button', { name: 'Voice transcription is not configured' }),
  ).toBeDisabled();
  await page.getByRole('button', { name: /New conversation/ }).click();
  await expect(page).toHaveURL(/\/chat\/[a-f0-9-]+$/);
  const composer = page.getByRole('textbox', { name: 'Message Finance Assistant' });
  await composer.fill('What is the outstanding balance for Asterion?');
  await page.getByRole('button', { name: 'Send message', exact: true }).click();
  await expect(page.getByText('INR 110,000.00', { exact: true }).first()).toBeVisible();
  await expect(page.getByText('FTI-00000022', { exact: true }).first()).toBeVisible();
  await page.getByRole('button', { name: /Inspect source records/ }).click();
  await expect(page.getByRole('dialog', { name: 'ERP evidence' })).toBeVisible();
  await expect(
    page
      .getByRole('dialog')
      .getByText('MOCK:MockCustomerOpenTransactions', { exact: true })
      .first(),
  ).toBeVisible();
  await page.keyboard.press('Escape');
  await composer.fill('Create a test customer called TEST-E2E-001.');
  await page.getByRole('button', { name: 'Send message', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Confirm action' })).toBeVisible();
  await page.getByRole('button', { name: 'Confirm action' }).click();
  await expect(
    page.getByText('The confirmed result is recorded in the audit history.'),
  ).toBeVisible();
  await page.reload();
  await expect(page.getByText('INR 110,000.00', { exact: true }).first()).toBeVisible();
  await expect(
    page.getByText('The confirmed result is recorded in the audit history.'),
  ).toBeVisible();
  await page.getByRole('button', { name: 'Open settings', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Workspace settings' });
  await dialog.getByRole('button', { name: 'Dynamics 365', exact: true }).click();
  await expect(dialog.getByText('Open transactions', { exact: true })).toBeVisible();
  await dialog.getByRole('button', { name: 'Audit history', exact: true }).click();
  await expect(dialog.getByText('Create customer', { exact: true })).toBeVisible();
  expect(errors).toEqual([]);
});

test('cancelled mock customer creation makes no ERP change', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: /New conversation/ }).click();
  const composer = page.getByRole('textbox', { name: 'Message Finance Assistant' });
  await composer.fill('Create a test customer called TEST-CANCEL-E2E.');
  await page.getByRole('button', { name: 'Send message', exact: true }).click();
  await page.getByRole('button', { name: 'Cancel', exact: true }).click();
  await expect(page.getByText('Cancelled. No ERP change was made.')).toBeVisible();
  await composer.fill('Find customer TEST-CANCEL-E2E');
  await page.getByRole('button', { name: 'Send message', exact: true }).click();
  await expect(
    page.getByText('No customer TEST-CANCEL-E2E was found in USMF.', { exact: true }),
  ).toBeVisible();
});
